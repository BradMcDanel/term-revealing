import torch
import torch.nn as nn
from torch.utils.cpp_extension import load

cgm_cuda = load(
    'cgm_cuda', ['kernels/cgm_cuda.cpp', 'kernels/cgm_cuda_kernel.cu'], extra_cflags=['-O3'])

class CGM(nn.Module):
    def __init__(self, group_size):
        super(CGM, self).__init__()
        self.group_size = group_size
    
    def forward(self, x):
        return cgm.apply(x, self.group_size)

    def extra_repr(self):
        return 'group_size={group_size}'.format(**self.__dict__)

class cgm(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, group_size):
        ctx.save_for_backward(x)
        if x.is_cuda:
            return cgm_cuda.forward(x, group_size)
        else:
            B, C, W, H = x.shape
            x = x.permute(2, 3, 0, 1).contiguous().view(-1, group_size)
            x[x != x.max(dim=1, keepdim=True)[0]] = 0
            return x.view(W, H, B, C).permute(2, 3, 0, 1).contiguous()

    @staticmethod
    def backward(ctx, grad_output):
        x, = ctx.saved_tensors
        if grad_output.is_cuda:
            grad_output = cgm_cuda.backward(grad_output, x)
        else:
            grad_output[x<0] = 0

        return grad_output, None