from __future__ import print_function
from tqdm import tqdm

import math
import numpy as np
import torch
import torch.nn as nn
from torch.nn.parallel import data_parallel
import torch.nn.functional as F
from torch.autograd import Variable
import time
import gc
import os


def adjust_learning_rate(optimizer, epoch, init_lr):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = init_lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


class AverageMeter(object):
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def train(model, train_loader, optimizer, epoch, loss):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()

    model.train()
    loss.train()
    end = time.time()
    model_loss = 0
    pbar = tqdm(train_loader, leave=False)
    for i, (data, target) in enumerate(pbar):
        batchsize = len(target)
        data_time.update(time.time() - end)
        if torch.cuda.is_available():
            data, target = data.cuda(), target.cuda()
        data, target = Variable(data), Variable(target)
        optimizer.zero_grad()
        prediction = data_parallel(model, data)
        loss_output = loss(prediction, target)

        if isinstance(loss_output, tuple):
            loss_value, outputs = loss_output
        else:
            loss_value = loss_output
        loss_value.backward()

        model_loss += batchsize*loss_value.item()
        loss_meter.update(loss_value.item(), batchsize)
        optimizer.step()

        pbar.set_postfix(loss='{loss_meter.avg:.4f}, comp={batch_time.avg:.4f}, data={data_time.avg:.4f}'.format(
                         loss_meter=loss_meter, batch_time=batch_time, data_time=data_time))

        batch_time.update(time.time() - end)
        end = time.time()

    optimizer.zero_grad()

    N = train_loader.num_samples
    model_loss = model_loss / N
    return model_loss

def test(model, test_loader, epoch, loss):
    batch_time = AverageMeter()
    data_time = AverageMeter()

    model.eval()
    loss.eval()
    end = time.time()
    num_correct, model_loss = 0, 0
    for i, (data, target) in enumerate(test_loader):
        batchsize = len(target)
        data_time.update(time.time() - end)
        if torch.cuda.is_available():
            data, target = data.cuda(), target.cuda()
        with torch.no_grad():
            data, target = Variable(data), Variable(target)
            prediction = data_parallel(model, data)
            loss_output = loss(prediction, target)

            if isinstance(loss_output, tuple):
                loss_value, outputs = loss_output
            else:
                loss_value = loss_output

            pred = prediction.data.max(1, keepdim=True)[1]
            correct = (pred.view(-1) == target.view(-1)).long().sum().item()
            num_correct += correct
            model_loss += batchsize * loss_value.sum()

        batch_time.update(time.time() - end)
        end = time.time()

    N = test_loader.num_samples
    model_loss = model_loss / N
    acc = 100. * (num_correct / N)

    return model_loss.item(), acc