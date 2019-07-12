import argparse
import os
import random
import shutil
import time
import warnings
from collections import OrderedDict
import pickle

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
import matplotlib
# matplotlib.use('Agg')
import matplotlib.pyplot as plt
SMALL_SIZE = 13
MEDIUM_SIZE = 17
BIGGER_SIZE = 20
matplotlib.rcParams["pdf.fonttype"] = 42
matplotlib.rcParams["ps.fonttype"] = 42

plt.rc("font", size=SMALL_SIZE)  # controls default text sizes
plt.rc("axes", titlesize=BIGGER_SIZE)  # fontsize of the axes title
plt.rc("axes", labelsize=MEDIUM_SIZE)  # fontsize of the x and y labels
plt.rc("xtick", labelsize=SMALL_SIZE)  # fontsize of the tick labels
plt.rc("ytick", labelsize=SMALL_SIZE)  # fontsize of the tick labels
plt.rc("legend", fontsize=SMALL_SIZE)  # legend fontsize
plt.rc('axes', titlesize=BIGGER_SIZE)  # fontsize of the figure title

import models
import cgm
import numpy as np


def evaluate(loader, model, args, name):
    # switch to evaluate mode
    model.eval()
    average_trackers = get_average_trackers(model)
    conflict_mats = []
    data_shapes = []
    num_samples = 0
    with torch.no_grad():
        for i, (images, target) in enumerate(loader):
            if i == 0:
                save_image(make_grid(images), 'figures/{}.png'.format(name))
            num_samples += len(target)
            images = images.cuda(0, non_blocking=True)
            target = target.cuda(0, non_blocking=True)
            _ = model(images)
            for k, tracker in enumerate(average_trackers):
                data = tracker.x.clone()
                data[data != 0] = 1

                # only care about conv layers for now
                if len(data.shape) < 4:
                    continue

                _, C, W, H = data.shape
                data = data.permute(1, 0, 2, 3).contiguous().view(C, -1)
                # first batch -- generate conflict mat
                if i == 0:
                    data_shapes.append((C, W, H))
                    conflict_mat = torch.mm(data, torch.transpose(data, 0, 1))
                    conflict_mats.append(conflict_mat)
                else:
                    conflict_mats[k] += torch.mm(data, torch.transpose(data, 0, 1))

    for i, conflict_mat in enumerate(conflict_mats):
        # remove diag 
        C, W, H = data_shapes[i]
        conflict_mat[range(len(conflict_mat)), range(len(conflict_mat))] = 0
        conflict_mat /= num_samples*W*H
    
    return conflict_mats


def rle(inarray):
    """ run length encoding. Partial credit to R rle function. 
        Multi datatype arrays catered for including non Numpy
        returns: tuple (runlengths, startpositions, values) """
    ia = np.asarray(inarray)                  # force numpy
    n = len(ia)
    if n == 0: 
        return (None, None, None)
    else:
        y = np.array(ia[1:] != ia[:-1])     # pairwise unequal (string safe)
        i = np.append(np.where(y), n - 1)   # must include last element posi
        z = np.diff(np.append(-1, i))       # run lengths
        p = np.cumsum(np.append(0, z))[:-1] # positions
        return(z, p, ia[i])

class AverageTracker(nn.Module):
    def __init__(self):
        super(AverageTracker, self).__init__()
        self.count = 0
        self.register_buffer('data', None)
        self.register_buffer('x', None)

    def forward(self, x):
        if self.data is None:
            self.data = torch.zeros(x.size()[1:]).cuda(0)
            self.zeros = torch.zeros(x.size()[1:]).cuda(0)
            self.x = torch.zeros(x.size()).cuda(0)
        self.x = x
        self.data += x.sum(0)
        self.count += x.size(0)
        self.zeros += (x == 0).float().sum(0)
        return x
    
    def avg_data(self):
        return self.data / self.count
    
    def channel_zeros(self):
        C, W, H = self.zeros.shape
        return self.zeros.sum((1, 2)) / float(W*H*self.count)

def add_average_trackers(model):
    for child_name, child in model.named_children():
        if isinstance(child, nn.ReLU):
            setattr(model, child_name, nn.Sequential(nn.ReLU(), AverageTracker()))
        else:
            add_average_trackers(child)

def get_average_trackers(model):
    average_trackers = []
    for child in model.children():
        if isinstance(child, AverageTracker):
            average_trackers.append(child)
        else:
            trackers = get_average_trackers(child)
            average_trackers.extend(trackers)
    
    return average_trackers

def get_conflict_score(conflict_mat, columns):
    from itertools import combinations 
    conflict_score = 0.0
    for i, j in combinations(columns, 2):
        conflict_score += conflict_mat[i][j].item()
    return conflict_score

def first_fit(conflict_mat, max_conflict_score=0.01, max_columns=8):
    bins = []
    for column in range(len(conflict_mat)):
        for bin_elems in bins:
            # cannot add to column as reached max multiplexing size
            if len(bin_elems) == max_columns:
                continue
            potential_bin = bin_elems + [column]
            conflict_score = get_conflict_score(conflict_mat, potential_bin)
            # valid bin found, add column to bin then break
            if conflict_score < max_conflict_score:
                bin_elems.append(column)
                break
        # no valid bin found, so create new bin
        else:
            bins.append([column])
    return bins



model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR', help='path to dataset')
parser.add_argument('resume', help='path to load model')
parser.add_argument('-a', '--arch', metavar='ARCH', default='alexnet',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('--msgpack-loader', dest='msgpack_loader', action='store_true',
                    help='use custom msgpack dataloader')
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--multiprocessing-distributed', action='store_true',
                    help='Use multi-processing distributed training to launch '
                         'N processes per node, which has N GPUs. This is the '
                         'fastest way to use PyTorch for either single node or '
                         'multi node data parallel training')

best_acc1 = 0


if __name__=='__main__':
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()

    # create model
    print("=> creating model '{}'".format(args.arch))
    model = models.__dict__[args.arch]()

    # optionally resume from a checkpoint
    if os.path.isfile(args.resume):
        print("=> loading checkpoint '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)
        tmp_state_dict = dict(checkpoint['state_dict'])
        state_dict = {}
        for key in tmp_state_dict.keys():
            if 'module.' in key:
                new_key = key.replace('module.', '')
            else:
                new_key = key
            state_dict[new_key] = tmp_state_dict[key]
        state_dict = OrderedDict(state_dict)
        model.load_state_dict(state_dict)
        print("=> loaded checkpoint '{}' (epoch {})"
              .format(args.resume, checkpoint['epoch']))
    else:
        print("=> no checkpoint found at '{}'".format(args.resume))
        assert False

    cudnn.benchmark = True

    ### begin custom data loader
    # using custom msgpack data loader due to slow disk, replace with standard
    # pytorch ImageFolder loader for equivalent results (shown below in else)
    if args.msgpack_loader:
        import pickle
        import msgpack
        from PIL import Image
        import io

        def msgpack_load(x):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                x = Image.open(io.BytesIO(x)).convert('RGB')
            return x

        class InMemoryImageNet(Dataset):
            def __init__(self, path, num_samples, transforms):
                self.path = path
                self.num_samples = num_samples
                self.transforms = transforms
                self.samples = []
                f = open(self.path, "rb")
                for i, sample in enumerate(msgpack.Unpacker(f, use_list=False, raw=True)):
                    x, label = sample
                    x = pickle.loads(x)
                    self.samples.append((x, label))
                    if i == self.num_samples - 1:
                        break
                f.close()
                
            def __getitem__(self, index):
                x, y = self.samples[index]
                x = self.transforms(x)
                return (x, y)

            def __len__(self):
                return self.num_samples

        train_path = os.path.join(args.data, 'imagenet-msgpack', 'ILSVRC-train-chunk.bin')
        val_path = os.path.join(args.data, 'imagenet-msgpack', 'ILSVRC-val.bin')
        num_train = 800 # just a small number for this test
        num_val = 800 # just a small number for this test
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                        std=[0.229, 0.224, 0.225])
        train_dataset = InMemoryImageNet(train_path, num_train,
                                transforms=transforms.Compose([
                                    msgpack_load,
                                    transforms.Resize(256),
                                    transforms.CenterCrop(224),
                                    transforms.ToTensor(),
                                    normalize,
                                ]))
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.batch_size,
                                                   shuffle=False, drop_last=False,
                                                   num_workers=args.workers)
        train_loader.num_samples = num_train
 

        val_dataset = InMemoryImageNet(val_path, num_val,
                                transforms=transforms.Compose([
                                    msgpack_load,
                                    transforms.Resize(256),
                                    transforms.CenterCrop(224),
                                    transforms.ToTensor(),
                                    normalize,
                                ]))
        val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=args.batch_size,
                                                 shuffle=False, drop_last=False,
                                                 num_workers=args.workers)
        val_loader.num_samples = num_val
    ### end custom data loader

    # Use standard PyTorch Dataloader
    else:  
        traindir = os.path.join(args.data, 'train')
        valdir = os.path.join(args.data, 'val')
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])

        train_dataset = datasets.ImageFolder(
            traindir,
            transforms.Compose([
                transforms.RandomResizedCrop(224),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                normalize,
            ]))

        if args.distributed:
            train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        else:
            train_sampler = None

        train_loader = torch.utils.data.DataLoader(
            train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
            num_workers=args.workers, pin_memory=True, sampler=train_sampler)

        val_loader = torch.utils.data.DataLoader(
            datasets.ImageFolder(valdir, transforms.Compose([
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                normalize,
            ])),
            batch_size=args.batch_size, shuffle=False,
            num_workers=args.workers, pin_memory=True)

    add_average_trackers(model)
    model.cuda(0)
    train_conflicts = evaluate(train_loader, model, args, 'train')
    val_conflicts = evaluate(val_loader, model, args, 'test')


    conflict_scores = np.linspace(0.0, 1.0, 50)
    for k in range(len(train_conflicts)):
        tc, vc = train_conflicts[k], val_conflicts[k]
        cc = np.corrcoef(tc.view(-1).cpu().numpy(), vc.view(-1).cpu().numpy())
        print('Correlation: {}'.format(cc))
        fig, axes = plt.subplots(nrows=1, ncols=2)
        im = axes[0].imshow(tc.tolist(), vmin=0, vmax=1)
        im = axes[1].imshow(vc.tolist(), vmin=0, vmax=1)

        fig.subplots_adjust(right=0.8)
        cbar_ax = fig.add_axes([0.85, 0.15, 0.05, 0.7])
        fig.colorbar(im, cax=cbar_ax)
        plt.savefig('figures/{}-val-sim-{}'.format(args.arch, k+1), dpi=300)
        plt.clf()
        
    #     total_bins = []
    #     for conflict_score in conflict_scores:
    #         print('Processing.. {}'.format(conflict_score))
    #         bins = first_fit(tc, conflict_score)
    #         total_bins.append(len(bins))
    #     layer_bin_data.append(total_bins)

    # for lidx, total_bins in enumerate(layer_bin_data):
    #     plt.plot(conflict_scores, total_bins, label='Layer {}'.format(lidx))

    # plt.xlabel('Max Conflict Score')
    # plt.ylabel('Number of Data Channels')
    # plt.legend(loc=0)
    # plt.xlim((-0.05, 1.05))
    # plt.tight_layout()
    # plt.savefig('figures/first_fit_conflicts.png', dpi=300)


    # # zeros = []
    # for i, l in enumerate(model.features):
    #     if type(l) == AverageTracker:
    #         z = l.channel_zeros()
    #         zeros.append(z.tolist())
    #         #plt.plot(torch.sort(z)[0].tolist(), '-o', linewidth=2)
    #         #plt.savefig('{}-{}-zeros.png'.format(args.arch, i))
    #         #plt.clf()

    # with open('{}.pkl'.format(args.arch), 'wb') as f:
    #     pickle.dump(zeros, f)
