import argparse
import math
import copy
import os
import random
import shutil
import time
import warnings
from collections import OrderedDict
import pickle
from itertools import product

import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
from torch.utils.data.dataset import Dataset
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from torchvision.utils import make_grid, save_image
import numpy as np
import json


import booth
import models
import util
plt = util.import_plt_settings(local_display=False)

def get_term_count(model, quant_func, minlength=None):
    if quant_func == 'hese':
        func = booth.num_hese_terms
    else:
        func = booth.num_binary_terms
    x = model.features[8][0].x
    w = model.features[8][1].weight.data
    B, C, W, H = x.shape
    F = w.shape[0]
    x = x.permute(0, 2, 3, 1).contiguous().view(-1, C)
    x_terms = func(x, 2**-7)
    x_terms = x_terms.view(B*W*H, C)
    w = w.view(F, C)
    w_terms = func(w, w_sfs[2])
    w_terms = w_terms.view(F, C)

    all_res = []
    for i in range(0, C//group_size):
        start, end = i*group_size, (i+1)*group_size
        res = torch.matmul(x_terms[:, start:end], w_terms[:, start:end].transpose(0, 1))
        all_res.extend(res.view(-1).tolist())

    all_res = np.array(all_res).astype(int)

    if minlength is None:
        return np.bincount(all_res)
    else:
        return np.bincount(all_res, minlength=minlength)

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR', help='path to dataset')
parser.add_argument('-a', '--arch', metavar='ARCH', default='alexnet',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--msgpack-loader', dest='msgpack_loader', action='store_true',
                    help='use custom msgpack dataloader')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--gpu', default=None, type=int, help='GPU id to use.')

if __name__=='__main__':
    args = parser.parse_args()
    model = models.__dict__[args.arch](pretrained=True)
    model.cuda()
    train_loader, train_sampler, val_loader = util.get_imagenet(args, 'ILSVRC-train-chunk.bin',
                                                                num_train=128, num_val=4)

    group_size = 8
    stat_terms = [8, 8, 4, 4]
    terms = [1000, 20, 1000, 16]
    quant_funcs = ['binary', 'binary', 'hese', 'hese']

    bcs = []
    for i, (term, stat_term, quant_func) in enumerate(zip(terms, stat_terms, quant_funcs)):
        qmodel = copy.deepcopy(model).cpu()
        w_sfs = util.quantize_layers(qmodel, bits=8)
        qmodel.cuda()
        qmodel = models.convert_model(qmodel, w_sfs, stat_term, 1, term, group_size, stat_term,
                                      1, term, group_size, 1000, fuse_bn=False, quant_func=quant_func)

        criterion = nn.CrossEntropyLoss().cuda()
        util.add_average_trackers(qmodel, nn.Conv2d)
        qmodel.cuda()
        _, acc = util.validate(val_loader, qmodel, criterion, args, verbose=True)
        bc = get_term_count(qmodel, quant_func)
        bcs.append(bc)

    fill_colors = ['cornflowerblue', 'tomato', 'cornflowerblue', 'tomato']
    names = ['Original, Binary', 'Term Revealing, Binary',
             'Original, HESE', 'Term Revealing, HESE']
    fig, axes = plt.subplots(4, sharex=True, sharey=True)
    max_x = len(bcs[0]) + 10
    max_y = 4.5
    for i, (ax, bc, fill_color, name) in enumerate(zip(axes, bcs, fill_colors, names)):
        xs = np.arange(len(bc))
        bc = 100. * (bc / bc.sum())
        ax.plot(xs, bc, '-', color='k', linewidth=1.0)
        plot = ax.fill_between(xs, bc, color=fill_color, edgecolor=fill_color)

        # ax.text(max_x, 3.4, name, fontsize=14, ha='right')

        # TODO: Add back annotations
        # if i == 0:
        #     ax.annotate('Max(232/512)', xy=(233.5, 0.0),  textcoords='data',
        #                 xytext=(232.5-50, 1.2), fontsize=12, arrowprops=dict(arrowstyle="-"))
        #     ax.plot([233.5], [0], 'or', markeredgecolor='k', ms=4)
        # elif i == 1:
        #     ax.annotate('Max(128/128)', xy=(127.5, 0.0), textcoords='data',
        #                 xytext=(128.5-30, 1.2), fontsize=12, arrowprops=dict(arrowstyle="-"))
        #     ax.plot([127.5], [0], 'or', markeredgecolor='k', ms=4)
        # elif i == 2:
        #     ax.annotate('Max(113/128)', xy=(112.5, 0.0), textcoords='data',
        #                 xytext=(113.5-30, 1.2), fontsize=12, arrowprops=dict(arrowstyle="-"))
        #     ax.plot([112.5], [0], 'or', markeredgecolor='k', ms=4)
        # elif i == 3:
        #     ax.annotate('Max(64/64)', xy=(63.5, 0.0), textcoords='data',
        #                 xytext=(64.5-10, 1.2), fontsize=12, arrowprops=dict(arrowstyle="-"))
        #     ax.plot([63.5], [0], 'or', markeredgecolor='k', ms=4)

    fig.text(0.02, 0.5, 'Frequency (%)', rotation=90, ha='center', va='center', fontsize=18)
    fig.subplots_adjust(hspace=0.0, wspace=0.0)
    axes[0].set_title('Term Pair Frequency per Group of 8')
    axes[3].set_xlabel('Number of Term Pairs')
    plt.savefig('figures/shiftnet.png', dpi=300, bbox_inches='tight')
    plt.clf()
