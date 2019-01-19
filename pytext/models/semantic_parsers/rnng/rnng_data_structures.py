#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

from typing import Any, List, Sized, Tuple

import torch as torch
import torch.nn as nn
from pytext.utils.cuda_utils import xaviervar


class Element:
    """
    Generic element representing a token / non-terminal / sub-tree on a stack.
    Used to compute valid actions in the RNNG parser.
    """

    def __init__(self, node: Any) -> None:
        self.node = node

    def __eq__(self, other) -> bool:
        return self.node == other.node

    def __repr__(self) -> str:
        return str(self.node)


class StackLSTM(Sized):
    """
    The Stack LSTM from Dyer et al: https://arxiv.org/abs/1505.08075
    """

    def __init__(
        self,
        lstm: nn.LSTM,
        initial_state: Tuple[torch.Tensor, torch.Tensor],
        empty_embedding: torch.Tensor,
    ):
        """
        Shapes:
            initial_state: (lstm_layers, 1, lstm_hidden_dim) each
            empty_embedding: (1, lstm_hidden_dim)
        """
        self.empty = empty_embedding
        self.lstm = lstm

        # Stack of (state, (embedding, element))
        self.stack = (
            [(initial_state, (self._lstm_output(initial_state), Element("Root")))]
            if initial_state
            else None
        )

    def _lstm_output(self, state: Tuple[torch.Tensor, torch.Tensor]) -> torch.Tensor:
        """
        Shapes:
            state: (lstm_layers, 1, lstm_hidden_dim) each
            return value: (1, lstm_hidden_dim)
        """
        return state[0][-1]

    def push(self, expression: torch.Tensor, element: Element) -> None:
        """
        Shapes:
            expression: (1, lstm_input_dim)
        """
        old_top_state = self.stack[-1][0]
        # Unsqueezing expression for sequence_length = 1
        _, new_top_state = self.lstm(expression.unsqueeze(0), old_top_state)
        # Push in (state, (embedding, element))
        self.stack.append((new_top_state, (self._lstm_output(new_top_state), element)))

    def pop(self) -> Tuple[torch.Tensor, Element]:
        """
        Pops and returns tuple of output embedding (1, lstm_hidden_dim) and element
        """

        return self.stack.pop()[1]

    def embedding(self) -> torch.Tensor:
        """
        Shapes:
            return value: (1, lstm_hidden_dim)
        """
        if len(self.stack) < 1:
            return self.empty

        top_state = self.stack[-1][0]
        return self._lstm_output(top_state)

    def element_from_top(self, index: int) -> Element:
        return self.stack[-(index + 1)][1][1]

    def __len__(self) -> int:
        return len(self.stack) - 1

    def __str__(self) -> str:
        return "->".join([str(x[1][1]) for x in self.stack])

    def copy(self):
        other = StackLSTM(self.lstm, None, self.empty)
        other.stack = list(self.stack)
        return other


class CompositionFunction(nn.Module):
    """
    Combines a list / sequence of embeddings into one
    """

    def __init__(self):
        super().__init__()


class CompositionalNN(CompositionFunction):
    """
    Combines a list / sequence of embeddings into one using a biLSTM
    """

    def __init__(self, lstm_dim: int):
        super().__init__()
        self.lstm_dim = lstm_dim
        self.lstm_fwd = nn.LSTM(lstm_dim, lstm_dim, num_layers=1)
        self.lstm_rev = nn.LSTM(lstm_dim, lstm_dim, num_layers=1)
        self.linear_seq = nn.Sequential(nn.Linear(2 * lstm_dim, lstm_dim), nn.Tanh())

    def forward(self, x: List[torch.Tensor]) -> torch.Tensor:
        """
        Embed the sequence. If the input corresponds to [IN:GL where am I at]:
        - x will contain the embeddings of [at I am where IN:GL] in that order.
        - Forward LSTM will embed the sequence [IN:GL where am I at].
        - Backward LSTM will embed the sequence [IN:GL at I am where].
        The final hidden states are concatenated and then projected.

        Args:
            x: Embeddings of the input tokens in *reversed* order
        Shapes:
            x: (1, lstm_dim) each
            return value: (1, lstm_dim)
        """
        # reset hidden state every time
        lstm_hidden_fwd = (
            xaviervar(1, 1, self.lstm_dim),
            xaviervar(1, 1, self.lstm_dim),
        )
        lstm_hidden_rev = (
            xaviervar(1, 1, self.lstm_dim),
            xaviervar(1, 1, self.lstm_dim),
        )
        nonterminal_element = x[-1]
        reversed_rest = x[:-1]
        # Always put nonterminal_element at the front
        fwd_input = [nonterminal_element] + reversed_rest[::-1]
        rev_input = [nonterminal_element] + reversed_rest
        stacked_fwd = self.lstm_fwd(torch.stack(fwd_input), lstm_hidden_fwd)[0][0]
        stacked_rev = self.lstm_rev(torch.stack(rev_input), lstm_hidden_rev)[0][0]
        combined = torch.cat([stacked_fwd, stacked_rev], dim=1)
        subtree_embedding = self.linear_seq(combined)
        return subtree_embedding


class CompositionalSummationNN(CompositionFunction):
    """
    Simpler version of CompositionalNN
    """

    def __init__(self, lstm_dim: int):
        super().__init__()
        self.lstm_dim = lstm_dim
        self.linear_seq = nn.Sequential(nn.Linear(lstm_dim, lstm_dim), nn.Tanh())

    def forward(self, x: List[torch.Tensor]) -> torch.Tensor:
        combined = torch.sum(torch.cat(x, dim=0), dim=0, keepdim=True)
        subtree_embedding = self.linear_seq(combined)
        return subtree_embedding


class ParserState:
    """
    Maintains state of the Parser. Useful for beam search
    """

    def __init__(self, parser=None):
        if not parser:
            return

        self.buffer_stackrnn = StackLSTM(
            parser.buff_rnn, parser.init_lstm(), parser.pempty_buffer_emb
        )
        self.stack_stackrnn = StackLSTM(
            parser.stack_rnn, parser.init_lstm(), parser.empty_stack_emb
        )
        self.action_stackrnn = StackLSTM(
            parser.action_rnn, parser.init_lstm(), parser.empty_action_emb
        )

        self.predicted_actions_idx = []
        self.action_scores = []

        self.num_open_NT = 0
        self.is_open_NT: List[bool] = []
        self.found_unsupported = False

        # negative cumulative log prob so sort(states) is in descending order
        self.neg_prob = 0

    def finished(self):
        return len(self.stack_stackrnn) == 1 and len(self.buffer_stackrnn) == 0

    def copy(self):
        other = ParserState()
        other.buffer_stackrnn = self.buffer_stackrnn.copy()
        other.stack_stackrnn = self.stack_stackrnn.copy()
        other.action_stackrnn = self.action_stackrnn.copy()
        other.predicted_actions_idx = self.predicted_actions_idx.copy()
        other.action_scores = self.action_scores.copy()
        other.num_open_NT = self.num_open_NT
        other.is_open_NT = self.is_open_NT.copy()
        other.neg_prob = self.neg_prob
        other.found_unsupported = self.found_unsupported
        return other

    def __gt__(self, other):
        return self.neg_prob > other.neg_prob

    def __eq__(self, other):
        return self.neg_prob == other.neg_prob
