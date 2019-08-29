from typing import Dict

from dataclasses import field

from sonosco.model.serialization import serializable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

import torch
import torch.nn as nn
import torch.nn.functional as F

from sonosco.models.attention import DotProductAttention
from sonosco.models.modules import supported_rnns

IGNORE_ID = -1


def pad_list(xs, pad_value):
    # From: espnet/src/nets/e2e_asr_th.py: pad_list()
    n_batch = len(xs)
    max_len = max(x.size(0) for x in xs)
    pad = xs[0].new(n_batch, max_len, *xs[0].size()[1:]).fill_(pad_value)
    for i in range(n_batch):
        pad[i, :xs[i].size(0)] = xs[i]
    return pad


@serializable
class Seq2Seq(nn.Module):
    """Sequence-to-Sequence architecture with configurable encoder and decoder.
    """
    encoder_args: Dict[str, str] = field(default_factory=dict)
    decoder_args: Dict[str, str] = field(default_factory=dict)


    def __post_init__(self):
        super(Seq2Seq, self).__init__()
        self.encoder = Encoder(**self.encoder_args)
        self.decoder = Decoder(**self.decoder_args)

    def forward(self, padded_input, input_lengths, padded_target):
        """
        Args:
            padded_input: N x Ti x D
            input_lengths: N
            padded_targets: N x To
        """
        encoder_padded_outputs, _ = self.encoder(padded_input, input_lengths)
        loss = self.decoder(padded_target, encoder_padded_outputs)
        return loss

    def recognize(self, input, input_length, char_list, args):
        """Sequence-to-Sequence beam search, decode one utterence now.
        Args:
            input: T x D
            char_list: list of characters
            args: args.beam

        Returns:
            nbest_hyps:
        """
        encoder_outputs, _ = self.encoder(input.unsqueeze(0), input_length)
        nbest_hyps = self.decoder.recognize_beam(encoder_outputs[0],
                                                 char_list,
                                                 args)
        return nbest_hyps


@serializable
class Encoder(nn.Module):
    r"""Applies a multi-layer LSTM to an variable length input sequence.
    """

    input_size: int
    hidden_size: int
    num_layers: int
    bidirectional: bool = True
    rnn_type: str = 'lstm'
    dropout: float = 0.0

    def __post_init__(self):
        super(Encoder, self).__init__()
        self.rnn = supported_rnns[self.rnn_type](self.input_size, self.hidden_size, self.num_layers,
                                                 batch_first=True,
                                                 dropout=self.dropout,
                                                 bidirectional=self.bidirectional)

    def forward(self, padded_input, input_lengths):
        """
        Args:
            padded_input: N x T x D
            input_lengths: N
        Returns: output, hidden
            - **output**: N x T x H
            - **hidden**: (num_layers * num_directions) x N x H
        """
        # Add total_length for supportting nn.DataParallel() later
        # see https://pytorch.org/docs/stable/notes/faq.html#pack-rnn-unpack-with-data-parallelism
        total_length = padded_input.size(1)  # get the max sequence length
        packed_input = pack_padded_sequence(padded_input, input_lengths,
                                            batch_first=True)
        packed_output, hidden = self.rnn(packed_input)
        output, _ = pad_packed_sequence(packed_output,
                                        batch_first=True,
                                        total_length=total_length)
        return output, hidden

    def flatten_parameters(self):
        self.rnn.flatten_parameters()


@serializable
class Decoder(nn.Module):
    """
    """

    # Hyper parameters
    # embedding + output
    vocab_size: int
    embedding_dim: int
    sos_id: int  # Start of Sentence
    eos_id: int  # End of Sentence
    hidden_size: int
    num_layers: int
    bidirectional_encoder: bool = True  # useless now

    # Components
    def __post_init__(self):
        super(Decoder, self).__init__()
        self.encoder_hidden_size = self.hidden_size
        self.embedding = nn.Embedding(self.vocab_size, self.embedding_dim)
        self.rnn = nn.ModuleList()

        self.rnn += [nn.LSTMCell(self.embedding_dim +
                                 self.encoder_hidden_size, self.hidden_size)]

        for l in range(1, self.num_layers):
            self.rnn += [nn.LSTMCell(self.hidden_size, self.hidden_size)]

        self.attention = DotProductAttention()

        self.mlp = nn.Sequential(
            nn.Linear(self.encoder_hidden_size + self.hidden_size,
                      self.hidden_size),
            nn.Tanh(),
            nn.Linear(self.hidden_size, self.vocab_size))

    def zero_state(self, encoder_padded_outputs, H=None):
        N = encoder_padded_outputs.size(0)
        H = self.hidden_size if H == None else H
        return encoder_padded_outputs.new_zeros(N, H)

    def forward(self, padded_input, encoder_padded_outputs):
        """
        Args:
            padded_input: N x To
            # encoder_hidden: (num_layers * num_directions) x N x H
            encoder_padded_outputs: N x Ti x H
        Returns:
        """
        # *********Get Input and Output
        # from espnet/Decoder.forward()
        # TODO: need to make more smart way
        ys = [y[y != IGNORE_ID] for y in padded_input]  # parse padded ys
        # prepare input and output word sequences with sos/eos IDs
        eos = ys[0].new([self.eos_id])
        sos = ys[0].new([self.sos_id])
        ys_in = [torch.cat([sos, y], dim=0) for y in ys]
        ys_out = [torch.cat([y, eos], dim=0) for y in ys]
        y_lens = [y.size(0) for y in ys_in]
        # padding for ys with -1
        # pys: utt x olen
        ys_in_pad = pad_list(ys_in, self.eos_id).type(torch.long)
        ys_out_pad = pad_list(ys_out, IGNORE_ID).type(torch.long)
        # print("ys_in_pad", ys_in_pad.size())
        assert ys_in_pad.size() == ys_out_pad.size()
        batch_size = ys_in_pad.size(0)
        output_length = ys_in_pad.size(1)
        # max_length = ys_in_pad.size(1) - 1  # TODO: should minus 1(sos)?

        # *********Init decoder rnn
        h_list = [self.zero_state(encoder_padded_outputs)]
        c_list = [self.zero_state(encoder_padded_outputs)]
        for l in range(1, self.num_layers):
            h_list.append(self.zero_state(encoder_padded_outputs))
            c_list.append(self.zero_state(encoder_padded_outputs))
        att_c = self.zero_state(encoder_padded_outputs,
                                H=encoder_padded_outputs.size(2))
        y_all = []

        # **********LAS: 1. decoder rnn 2. attention 3. concate and MLP
        embedded = self.embedding(ys_in_pad)
        for t in range(output_length):
            # step 1. decoder RNN: s_i = RNN(s_i−1,y_i−1,c_i−1)
            rnn_input = torch.cat((embedded[:, t, :], att_c), dim=1)
            h_list[0], c_list[0] = self.rnn[0](
                rnn_input, (h_list[0], c_list[0]))
            for l in range(1, self.num_layers):
                h_list[l], c_list[l] = self.rnn[l](
                    h_list[l - 1], (h_list[l], c_list[l]))
            rnn_output = h_list[-1]  # below unsqueeze: (N x H) -> (N x 1 x H)
            # step 2. attention: c_i = AttentionContext(s_i,h)
            att_c, att_w = self.attention(rnn_output.unsqueeze(dim=1),
                                          encoder_padded_outputs)
            att_c = att_c.squeeze(dim=1)
            # step 3. concate s_i and c_i, and input to MLP
            mlp_input = torch.cat((rnn_output, att_c), dim=1)
            predicted_y_t = self.mlp(mlp_input)
            y_all.append(predicted_y_t)

        y_all = torch.stack(y_all, dim=1)  # N x To x C
        model_out = y_all
        # **********Cross Entropy Loss
        # F.cross_entropy = NLL(log_softmax(input), target))
        y_all = y_all.view(batch_size * output_length, self.vocab_size)
        ce_loss = F.cross_entropy(y_all, ys_out_pad.view(-1),
                                  ignore_index=IGNORE_ID,
                                  reduction='elementwise_mean')
        # TODO: should minus 1 here ?
        # ce_loss *= (np.mean([len(y) for y in ys_in]) - 1)
        # print("ys_in\n", ys_in)
        # temp = [len(x) for x in ys_in]
        # print(temp)
        # print(np.mean(temp) - 1)
        return model_out, y_lens, ce_loss

        # *********step decode
        # decoder_outputs = []
        # sequence_symbols = []
        # lengths = np.array([max_length] * batch_size)

        # def decode(step, step_output, step_attn):
        #     # step_output is log_softmax()
        #     decoder_outputs.append(step_output)
        #     symbols = decoder_outputs[-1].topk(1)[1]
        #     sequence_symbols.append(symbols)
        #     #
        #     eos_batches = symbols.data.eq(self.eos_id)
        #     if eos_batches.dim() > 0:
        #         eos_batches = eos_batches.cpu().view(-1).numpy()
        #         update_idx = ((step < lengths) & eos_batches) != 0
        #         lengths[update_idx] = len(sequence_symbols)
        #     return symbols

        # # *********Run each component
        # decoder_input = ys_in_pad
        # embedded = self.embedding(decoder_input)
        # rnn_output, decoder_hidden = self.rnn(embedded)  # use zero state
        # output, attn = self.attention(rnn_output, encoder_padded_outputs)
        # output = output.contiguous().view(-1, self.hidden_size)
        # predicted_softmax = F.log_softmax(self.out(output), dim=1).view(
        #     batch_size, output_length, -1)
        # for t in range(predicted_softmax.size(1)):
        #     step_output = predicted_softmax[:, t, :]
        #     step_attn = attn[:, t, :]
        #     decode(t, step_output, step_attn)

    def recognize_beam(self, encoder_outputs, char_list, args):
        """Beam search, decode one utterence now.
        Args:
            encoder_outputs: T x H
            char_list: list of character
            args: args.beam
        Returns:
            nbest_hyps:
        """
        # search params
        beam = args['beam_size']
        nbest = args['nbest']
        if args['decode_max_len'] == 0:
            maxlen = encoder_outputs.size(0)
        else:
            maxlen = args['decode_max_len']

        # *********Init decoder rnn
        h_list = [self.zero_state(encoder_outputs.unsqueeze(0))]
        c_list = [self.zero_state(encoder_outputs.unsqueeze(0))]
        for l in range(1, self.num_layers):
            h_list.append(self.zero_state(encoder_outputs.unsqueeze(0)))
            c_list.append(self.zero_state(encoder_outputs.unsqueeze(0)))
        att_c = self.zero_state(encoder_outputs.unsqueeze(0),
                                H=encoder_outputs.unsqueeze(0).size(2))
        # prepare sos
        y = self.sos_id
        vy = encoder_outputs.new_zeros(1).long()

        hyp = {'score': 0.0, 'yseq': [y], 'c_prev': c_list, 'h_prev': h_list,
               'a_prev': att_c}
        hyps = [hyp]
        ended_hyps = []

        for i in range(maxlen):
            hyps_best_kept = []
            for hyp in hyps:
                # vy.unsqueeze(1)
                vy[0] = hyp['yseq'][i]
                embedded = self.embedding(vy)
                # embedded.unsqueeze(0)
                # step 1. decoder RNN: s_i = RNN(s_i−1,y_i−1,c_i−1)
                rnn_input = torch.cat((embedded, hyp['a_prev']), dim=1)
                h_list[0], c_list[0] = self.rnn[0](
                    rnn_input, (hyp['h_prev'][0], hyp['c_prev'][0]))
                for l in range(1, self.num_layers):
                    h_list[l], c_list[l] = self.rnn[l](
                        h_list[l - 1], (hyp['h_prev'][l], hyp['c_prev'][l]))
                rnn_output = h_list[-1]
                # step 2. attention: c_i = AttentionContext(s_i,h)
                # below unsqueeze: (N x H) -> (N x 1 x H)
                att_c, att_w = self.attention(rnn_output.unsqueeze(dim=1),
                                              encoder_outputs.unsqueeze(0))
                att_c = att_c.squeeze(dim=1)
                # step 3. concate s_i and c_i, and input to MLP
                mlp_input = torch.cat((rnn_output, att_c), dim=1)
                predicted_y_t = self.mlp(mlp_input)
                local_scores = F.log_softmax(predicted_y_t, dim=1)
                # topk scores
                local_best_scores, local_best_ids = torch.topk(
                    local_scores, beam, dim=1)

                for j in range(beam):
                    new_hyp = {}
                    new_hyp['h_prev'] = h_list[:]
                    new_hyp['c_prev'] = c_list[:]
                    new_hyp['a_prev'] = att_c[:]
                    new_hyp['score'] = hyp['score'] + local_best_scores[0, j]
                    new_hyp['yseq'] = [0] * (1 + len(hyp['yseq']))
                    new_hyp['yseq'][:len(hyp['yseq'])] = hyp['yseq']
                    new_hyp['yseq'][len(hyp['yseq'])] = int(
                        local_best_ids[0, j])
                    # will be (2 x beam) hyps at most
                    hyps_best_kept.append(new_hyp)

                hyps_best_kept = sorted(hyps_best_kept,
                                        key=lambda x: x['score'],
                                        reverse=True)[:beam]
            # end for hyp in hyps
            hyps = hyps_best_kept

            # add eos in the final loop to avoid that there are no ended hyps
            if i == maxlen - 1:
                for hyp in hyps:
                    hyp['yseq'].append(self.eos_id)

            # add ended hypothes to a final list, and removed them from current hypothes
            # (this will be a probmlem, number of hyps < beam)
            remained_hyps = []
            for hyp in hyps:
                if hyp['yseq'][-1] == self.eos_id:
                    # hyp['score'] += (i + 1) * penalty
                    ended_hyps.append(hyp)
                else:
                    remained_hyps.append(hyp)

            hyps = remained_hyps
            # if len(hyps) > 0:
            #     print('remeined hypothes: ' + str(len(hyps)))
            # else:
            #     print('no hypothesis. Finish decoding.')
            #     break
            #
            # for hyp in hyps:
            #     print('hypo: ' + ''.join([char_list[int(x)]
            #                               for x in hyp['yseq'][1:]]))
        # end for i in range(maxlen)
        nbest_hyps = sorted(ended_hyps, key=lambda x: x['score'], reverse=True)[
                     :min(len(ended_hyps), nbest)]
        return nbest_hyps
