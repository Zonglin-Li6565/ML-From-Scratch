"""
The memory module of DNC
"""
import torch
import torch.nn as nn
import torch.nn.functional as functional
from torch.nn import Module, Parameter
from torch.autograd import Variable


class Memory(Module):
    """The memory module for DNC

    The memory module is only for one data point in a batch.
    """

    def __init__(self, memory_size, num_read_heads, batch_size):
        """Create a memory module.

        The memory module will be responsible for read write to the memory
        (The addressing, allocation, temporal link).

        Args:
            memory_size (Tuple[int, int]): The size of the memory
            num_read_heads (int): The number of read heads
            batch_size (int): The batch size
        """
        super().__init__()
        num_cells, cell_size = memory_size
        self.memory_size = memory_size
        self.num_read_heads = num_read_heads
        self.batch_size = batch_size
        self.memory = Parameter(
            torch.Tensor((batch_size, num_cells, cell_size)),
            requires_grad=False)
        self.temporal_link = Parameter(
            torch.Tensor((batch_size, num_cells, num_cells)),
            requires_grad=False)
        self.usage = Parameter(
            torch.Tensor(batch_size, 1, num_cells), requires_grad=False)
        self.allocation = Parameter(
            torch.Tensor(batch_size, 1, num_cells), requires_grad=False)
        self.precedence = Parameter(
            torch.Tensor(batch_size, 1, num_cells), requires_grad=False)
        self.read_weights = Parameter(
            torch.Tensor(batch_size, num_read_heads, num_cells),
            requires_grad=False)
        self.reset()

    def reset(self):
        """Resets the memory module

        Zeros out memory, usage vector, temporal links, allocation vector,
        precedence vector and read weights.
        """
        self.memory.data.zero_()
        self.usage.data.zero_()
        self.temporal_link.zero_()
        self.allocation.zero_()
        self.precedence.zero_()
        self.read_weights.zero_()

    def write_addressing_(self, key, strength):
        """Compute the content based addressing as described in the paper

        Args:
            key (torch.Tensor): The write key. Should be a tensor with shape
                (Batch, W)
            strength (torch.Tensor): The write weight. A tensor with shape
                (Batch, 1)

        Returns:
            torch.Tensor: The probability for each row to be written. The shape
                is (Batch, 1, W)
        """
        cos_similarity = nn.CosineSimilarity()
        key = key.unsqueeze(dim=1)
        similarities = [
            functional.softmax(
                cos_similarity(key[i], self.memory[i]).unsqueeze(dim=0) *
                strength[i],
                dim=0) for i in range(self.batch_size)
        ]
        return torch.stack(similarities, dim=0)

    def read_addressing_(self, key, strength):
        """Computes the read content addressing for the whole batch

        Args:
            key (torch.Tensor): The read keys for all batches for all read
                heads. Shape should be (Batch, #RH, W)
            strength (torch.Tensor): Shape should be (Batch, #RH, 1)

        Returns:
            torch.Tensor: The probability for each row to be read by each read
                head. The shape is (Batch, #RH, W)
        """
        cos_similarity = nn.CosineSimilarity()
        key = key.unsqueeze(dim=2)
        addressing = []
        for i in range(self.batch_size):
            batch_key = key[i]
            batch_weight = strength[i]
            similarities = [
                functional.softmax(
                    cos_similarity(batch_key[j], self.memory[i]), dim=0)
                for j in range(self.num_read_heads)
            ]
            similarities = torch.stack(similarities, dim=0) * batch_weight
            addressing.append(similarities)
        return torch.stack(addressing, dim=0)

    def get_allocation_(self):
        """Computes the allocation weightings as described in the paper

        Updates saved allocation vectors.

        Returns:
            torch.Tensor: The allocation weight for each memory entry (row in
                the memory matrix)
        """
        _, idx = torch.sort(self.usage, dim=2)
        multiply = Variable(self.usage.new_ones(self.batch_size, 1))
        for i in range(idx.shape[2]):
            self.allocation[:, :, i] = (1 - self.usage[:, :, i]) * multiply
            multiply *= self.usage[:, :, i]
        return self.allocation

    def get_read_weight_(self, interface):
        """Computes the new read weight from the old read weight and the
           temporal link

        Updates saved read weights.

        Args:
            interface (Interface): The interface object created from the
                interface vector for the whole batch.

        Returns:
            torch.Tensor: The read weight for each each head for each batch. The
                shape will be: (Batch, #RH, N)
        """
        transpose_temporal_link = torch.transpose(self.temporal_link, 1, 2)
        forward_weights = torch.bmm(self.read_weights, transpose_temporal_link)
        backward_weights = torch.bmm(self.read_weights, self.temporal_link)
        content_weights = self.read_addressing_(interface.read_keys,
                                                interface.read_strength)
        forward_modes = interface.read_modes[:, :, 0].unsqueeze(dim=2)
        backward_modes = interface.read_modes[:, :, 1].unsqueeze(dim=2)
        content_modes = interface.read_modes[:, :, 2].unsqueeze(dim=2)
        self.read_weights = (
            forward_weights * forward_modes + backward_weights * backward_modes
            + content_weights * content_modes)
        return self.read_weights

    def forward(self, interface):
        """Performs one read and write cycle

        The following steps are deduced from the formulas in the paper:
            1. Compute forward and backward weights
            2. Compute the read weight
            3. Perform read
            4. Computes the allocation weights
            5. Computes the weight weights using allocation weights and
               content weights.
            6. Write to the memory matrix
            7. Update the temporal link matrix
            8. Update usage vector

        Note that the interface vectors here are all row vectors,
        unlike in the paper

        Args:
            interface (Interface): The interface emitted from the controller.
                Shape: (Batch, W * R + 3 * W + 5 * R + 3)

        Returns:
            torch.Tensor: The read result. Shape: (Batch, #RH, W)
        """

        # Read

        read_weights = self.get_read_weight_(interface)
        read_result = torch.bmm(read_weights, self.memory)

        # Compute allocation weights

        allocation_weight = self.get_allocation_()
        write_content_address = self.write_addressing_(interface.write_key,
                                                       interface.write_strength)

        # Perform the write

        write_weights = (
            interface.allocation_gate *
            (allocation_weight - write_content_address) + write_content_address)
        write_weights *= interface.write_gate

        write_vector = interface.write_vector
        erase_vector = interface.erase_vector
        transposed_write_weights = torch.transpose(write_weights, 1, 2)

        self.memory *= 1 - torch.bmm(transposed_write_weights, erase_vector)
        self.memory += torch.bmm(transposed_write_weights, write_vector)

        # Update the temporal linkage

        num_cells = self.memory_size[0]
        grid_sum = (transposed_write_weights.repeat(1, 1, num_cells) +
                    write_weights.repeat(1, num_cells, 1))
        grid_subtract = 1 - grid_sum
        self.temporal_link = (grid_subtract * self.temporal_link + torch.bmm(
            transposed_write_weights, self.precedence))
        self.precedence = (
            (1 - torch.sum(write_weights)) * self.precedence + write_weights)

        # Update usage vector

        retention_vector = Variable(
            self.usage.new_ones(self.batch_size, 1, num_cells))
        free_gates = interface.free_gates
        old_read_weights = self.read_weights
        for i in range(self.num_read_heads):
            retention_vector *= (
                1 - free_gates[:, i, :] * old_read_weights[:, i, :]
            ).unsqueeze(dim=1)
        old_usage = self.usage
        self.usage = (old_usage + write_weights -
                      old_usage * write_weights) * retention_vector

        return read_result
