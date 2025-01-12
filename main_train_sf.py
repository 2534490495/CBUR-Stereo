# from __future__ import print_function, division
import argparse
import os
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import torchvision.utils as vutils
import torch.nn.functional as F
import numpy as np
import time
from tensorboardX import SummaryWriter
from datasets import __datasets__
# from models import __models__, model_loss_train_attn_only, model_loss_train_freeze_attn, model_loss_train, model_loss_test
from utils import *
from torch.utils.data import DataLoader
import gc
# from apex import amp
import cv2
from datasets import sceneflow_listfile

from models.G2L import G2L
from tqdm import tqdm

import torch
import torch.nn.functional as F
import shutil

def adjust_learning_rate_cos(optimizer, epoch_idx, max_lr, min_lr, section):
    splits=section.split(',')
    assert len(splits) == 2
    start_epoch=int(splits[0])
    end_epoch=int(splits[1])
    
    if epoch_idx<start_epoch:
        lr=max_lr
    elif epoch_idx>=start_epoch and epoch_idx<end_epoch:
        lr=min_lr+(max_lr-min_lr)*(1/2)*(1+math.cos(math.pi*(epoch_idx-start_epoch)/(end_epoch-start_epoch)))
    else:
        lr=min_lr

    print("setting learning rate to {}".format(lr))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr 

def adjust_learning_rate_adapt(optimizer,scheduler,val_object,min_lr):    
    
    scheduler.step(val_object)
    lr = optimizer.param_groups[0]['lr']
    if lr < min_lr:
        for param_group in optimizer.param_groups:
            param_group['lr'] = min_lr 

def train():

    for epoch_idx in range(start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch_idx, args.lr, args.lrepochs)
        # adjust_learning_rate_cos(optimizer,epoch_idx,args.lr,args.min_lr,args.cos_section)
        print("setting learning rate to {}".format(optimizer.param_groups[0]['lr']))
        
        # training
        avg_train_scalars = AverageMeterDict()
        
        with tqdm(total=len(TrainImgLoader),desc="Train epoch {}/{}".format(epoch_idx,args.epochs)) as pbar:
            for batch_idx, sample in enumerate(TrainImgLoader):
                start_time = time.time()
                loss, scalar_outputs = train_sample(sample)
                avg_train_scalars.update(scalar_outputs)
                del scalar_outputs 
                pbar.update(1)
                pbar.set_postfix({'train loss':'{:.3f}'.format(loss),'time':'{:.3f}'.format(time.time() - start_time)})
        
        avg_train_scalars = avg_train_scalars.mean()
        save_scalars(logger, 'fulltrain', avg_train_scalars, (epoch_idx + 1))
        save_scalars(logger, 'fulltrain', {'lr':optimizer.param_groups[0]['lr']}, (epoch_idx + 1))
        with open(os.path.join(args.logdir,'train_result.txt'),'a') as file:
            file.write(f'epoch_idx:{epoch_idx+1}\n')
            for k,v in avg_train_scalars.items():
                file.write(f'{k}:{v}\n')
            file.write('lr:{}\n'.format(optimizer.param_groups[0]['lr']))
            file.write('\n')
        gc.collect()

        # testing
        avg_test_scalars = AverageMeterDict()
        
        with tqdm(total=len(TestImgLoader),desc="Test epoch {}/{}".format(epoch_idx,args.epochs)) as pbar:
            for batch_idx, sample in enumerate(TestImgLoader):    
                start_time = time.time()
                loss, scalar_outputs = test_sample(sample)
                avg_test_scalars.update(scalar_outputs)
                del scalar_outputs
                pbar.update(1)
                pbar.set_postfix({'test loss':'{:.3f}'.format(loss),'time':'{:.3f}'.format(time.time() - start_time)})

        avg_test_scalars = avg_test_scalars.mean()
        save_scalars(logger, 'fulltest', avg_test_scalars, (epoch_idx + 1))
        with open(os.path.join(args.logdir,'test_result.txt'),'a') as file:
            file.write(f'epoch_idx:{epoch_idx+1}\n')
            for k,v in avg_test_scalars.items():
                file.write(f'{k}:{v}\n')
            file.write('\n')
        print("avg_test_scalars", avg_test_scalars)
        gc.collect() 
               
        # saving checkpoints
        if (epoch_idx + 1) % args.save_freq == 0:
            checkpoint_data = {'epoch': epoch_idx, 'model': model.state_dict(), 'optimizer': optimizer.state_dict()}
            checkpoint_data['scheduler']=scheduler.state_dict()
            #id_epoch = (epoch_idx + 1) % 100
            torch.save(checkpoint_data, "{}/checkpoint_{:0>6}.ckpt".format(args.logdir, epoch_idx))

            # #ckpt通信
            # torch.save(checkpoint_data, "./temp_sf/checkpoint_{:0>6}.ckpt".format(epoch_idx))

def train_sample(sample,compute_metrics=False):
    model.train()
    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    if torch.cuda.is_available() and args.cuda:
        imgL = imgL.cuda()
        imgR = imgR.cuda()
        disp_gt = disp_gt.cuda()
    optimizer.zero_grad()
    disp_ests = model(imgL, imgR, training=True)

    losses=[]
    if args.only_disp4:
        train_weights=[0,0,1,0.3]
    elif args.freezen_disp4:
        train_weights=[1,0.3,0,0]
    else:
        train_weights=[1,0.07,0.07,0.23]

    for i,disp_est in enumerate(disp_ests[:-1]):
        if disp_est.shape[1]!=disp_gt.shape[1]:
            disp_est=F.interpolate(disp_est.unsqueeze(1),size=(disp_gt.shape[1], disp_gt.shape[2]),mode='bilinear').squeeze(1)
        mask = ((disp_gt < args.maxdisp) & (disp_gt > 0)).detach_()
        losses.append(F.smooth_l1_loss(disp_est[mask], disp_gt[mask], reduction='mean'))

    loss = sum([losses[i] * train_weights[i] for i in range(len(disp_ests[:-1]))]) /\
        sum([1 * train_weights[i] for i in range(len(disp_ests[:-1]))])

    scalar_outputs = {"loss": loss}
    for i in range(len(losses)):
        scalar_outputs["{}th_loss".format(i)] = losses[i]
    
    with torch.no_grad():
        mask = ((disp_gt < args.maxdisp) & (disp_gt > 0)).detach_()
        scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask) for disp_est in disp_ests[:1]]
        scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask) for disp_est in disp_ests[:1]]
        scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0) for disp_est in disp_ests[:1]]
        scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0) for disp_est in disp_ests[:1]]
        scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0) for disp_est in disp_ests[:1]]

    loss.backward()
    optimizer.step()
    return tensor2float(loss), tensor2float(scalar_outputs) 


# test one sample
@make_nograd_func
def test_sample(sample, compute_metrics=True):
    model.eval()
    imgL, imgR, disp_gt = sample['left'], sample['right'], sample['disparity']
    if torch.cuda.is_available() and args.cuda:
        imgL = imgL.cuda()
        imgR = imgR.cuda()
        disp_gt = disp_gt.cuda()

    disp_est = model(imgL, imgR)[0]

    mask = (disp_gt < args.maxdisp) & (disp_gt > 0)
    if mask.float().mean()<0.01:
        loss=torch.tensor(0, dtype=torch.float32, device=disp_gt.device)
    else:
        loss = F.smooth_l1_loss(disp_est[mask], disp_gt[mask], reduction='mean')

    scalar_outputs = {"loss": loss}
    scalar_outputs["EPE"] = [EPE_metric(disp_est, disp_gt, mask)]
    scalar_outputs["D1"] = [D1_metric(disp_est, disp_gt, mask)]
    scalar_outputs["Thres1"] = [Thres_metric(disp_est, disp_gt, mask, 1.0)]
    scalar_outputs["Thres2"] = [Thres_metric(disp_est, disp_gt, mask, 2.0)]
    scalar_outputs["Thres3"] = [Thres_metric(disp_est, disp_gt, mask, 3.0)]

    return tensor2float(loss), tensor2float(scalar_outputs)


if __name__ == '__main__':
    cudnn.benchmark = True
    os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3'

    parser = argparse.ArgumentParser(description='Neighborhood-Similarity Guided Disparity Refinement in Lightweight Stereo Matching')
    # parser.add_argument('--model', default='acvnet', help='select a model structure', choices=__models__.keys())
    parser.add_argument('--maxdisp', type=int, default=192, help='maximum disparity')
    parser.add_argument('--dataset', default='sceneflow', help='dataset name', choices=__datasets__.keys())
    parser.add_argument('--datapath', default="../SceneFlow", help='data path')
    parser.add_argument('--logdir',default='./log', help='the directory to save logs and checkpoints')
    # parser.add_argument('--trainlist', default='./filenames/sceneflow_train.txt', help='training list')
    # parser.add_argument('--testlist',default='./filenames/sceneflow_test.txt', help='testing list')
    parser.add_argument('--lr', type=float, default=0.001, help='base learning rate')
    parser.add_argument('--min_lr', type=float, default=0.0001, help='min learning rate')
    parser.add_argument('--epochs', type=int, default=20, help='number of epochs to train')
    parser.add_argument('--cos_section',default="17,26", type=str,  help='the section epochs for cos decay')
    parser.add_argument('--lrepochs',default="14,18:3", type=str,  help='the epochs to decay lr: the downscale rate')
    parser.add_argument('--patience', type=int, default=5, help='number of epochs to wait')
    parser.add_argument('--factor', type=float, default=0.3, help='adapt down rate')

    parser.add_argument('--batch_size', type=int, default=8, help='training batch size')
    parser.add_argument('--test_batch_size', type=int, default=4, help='testing batch size')
    parser.add_argument('--num_workers', type=int, default=8, help='num_workers')

    parser.add_argument('--only_disp4', default=False, action='store_true',  help='only train disp4 weights')
    parser.add_argument('--freezen_disp4', default=False, action='store_true',  help='freeze disp4 weights parameters')
    parser.add_argument('--whole_with_ckpt', default=False, action='store_true',  help='train the whole model with ckpt')
    parser.add_argument('--loadckpt', default='./pretrained_model/pretrained_model_sceneflow.ckpt',help='load the weights from a specific checkpoint')
    parser.add_argument('--mode', default='RGB', help='train data color, RGB or L')

    parser.add_argument('--resume', action='store_true', help='continue training the model')
    parser.add_argument('--seed', type=int, default=42, metavar='S', help='random seed (default: 1)')
    # parser.add_argument('--summary_freq', type=int, default=100, help='the frequency of saving summary')
    parser.add_argument('--save_freq', type=int, default=1, help='the frequency of saving checkpoint')
    parser.add_argument('--cuda', default=True, action='store_true', help='use cuda to train the model')

    # parse arguments, set seeds
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    if torch.cuda.is_available() and args.cuda:
        torch.cuda.manual_seed(args.seed)

    # create summary logger
    os.makedirs(args.logdir, exist_ok=True)
    print("creating new summary file")
    current_time=time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime())
    if not args.resume:
        args.logdir=os.path.join(args.logdir,current_time)
    logger = SummaryWriter(args.logdir)

    #保存命令行参数
    with open(os.path.join(args.logdir,'config.txt'),'a') as file:
        for k,v in vars(args).items():
            file.write(f'{k}:{v}\n')
        file.write('\n')

    # dataset, dataloader
    StereoDataset = __datasets__[args.dataset]
    train_dataset = StereoDataset(args.datapath, True, mode=args.mode)
    test_dataset = StereoDataset(args.datapath, False, mode=args.mode)
    TrainImgLoader = DataLoader(train_dataset, args.batch_size, shuffle=True, num_workers=args.num_workers, drop_last=True)
    TestImgLoader = DataLoader(test_dataset, args.test_batch_size, shuffle=False, num_workers=args.num_workers, drop_last=False)

    # model
    model = G2L(only_disp4=args.only_disp4, freezen_disp4=args.freezen_disp4)
    model = nn.DataParallel(model)
    if torch.cuda.is_available() and args.cuda:
        model.cuda()

    #加载参数
    if (args.freezen_disp4 or args.whole_with_ckpt):
        print("loading model {}".format(args.loadckpt))
        state_dict = torch.load(args.loadckpt)
        model_dict = model.state_dict()
        pre_dict = {k: v for k, v in state_dict['model'].items() if k in model_dict and model_dict[k].shape==state_dict['model'][k].shape}
        model_dict.update(pre_dict) 
        model.load_state_dict(model_dict)
    
    # optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr, betas=(0.9, 0.999))
    scheduler=optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=args.patience, factor=args.factor)

    # load parameters
    start_epoch = 0
    if args.resume:
        # find all checkpoints file and sort according to epoch id
        all_saved_ckpts = [fn for fn in os.listdir(args.logdir) if fn.endswith(".ckpt")]
        all_saved_ckpts = sorted(all_saved_ckpts, key=lambda x: int(x.split('_')[-1].split('.')[0]))
        # use the latest checkpoint file
        loadckpt = os.path.join(args.logdir, all_saved_ckpts[-1])
        print("loading the lastest model in logdir: {}".format(loadckpt))
        state_dict = torch.load(loadckpt)
        model.load_state_dict(state_dict['model'])
        optimizer.load_state_dict(state_dict['optimizer'])
        scheduler.load_state_dict(state_dict['scheduler'])
        start_epoch = state_dict['epoch'] + 1

    print("start at epoch {}".format(start_epoch))

    train()
