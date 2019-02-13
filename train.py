import argparse

import datasets
from model import FOTSModel
import torch
import torch.utils.data
import numpy as np
import os
import math
import tqdm


def restore_checkpoint(folder):
    model = FOTSModel().to(torch.device("cuda"))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.005, weight_decay=1e-5)
    lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=20, verbose=True, threshold=0.0001, threshold_mode='rel', cooldown=0, min_lr=0, eps=1e-08)

    if os.path.isfile(os.path.join(folder, 'last_checkpoint.pt')):
        checkpoint = torch.load(os.path.join(folder, 'last_checkpoint.pt'))
        epoch = checkpoint['epoch'] + 1
        model.load_state_dict(checkpoint['model_state_dict'])

        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        lr_scheduler.load_state_dict(checkpoint['lr_scheduler_state_dict'])
        best_score = checkpoint['best_score']
        return epoch, model, optimizer, lr_scheduler, best_score
    else:
        return 0, model, optimizer, lr_scheduler, +math.inf


def save_checkpoint(epoch, model, optimizer, lr_scheduler, best_score, folder, save_as_best):
    if not os.path.exists(folder):
        os.makedirs(folder)
    if save_as_best:
        torch.save(model.state_dict(), os.path.join(folder, 'best_model.pt'))
        print('Updated best_model')
    torch.save({
        'epoch': epoch,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'lr_scheduler_state_dict': lr_scheduler.state_dict(),
        'best_score': best_score  # not current score
    }, os.path.join(folder, 'last_checkpoint.pt'))


def detection_loss(pred, gt):
    y_pred_cls, y_pred_geo, theta_pred = pred
    y_true_cls, y_true_geo, theta_gt, training_mask, file_names = gt
    y_true_cls, theta_gt = y_true_cls.unsqueeze(1), theta_gt.unsqueeze(1)
    y_true_cls, y_true_geo, theta_gt, training_mask = y_true_cls.to('cuda'), y_true_geo.to('cuda'), theta_gt.to('cuda'), training_mask.to('cuda')

    OHEM_mask = torch.ones_like(y_true_cls)  # TODO OHEM
    mask = OHEM_mask * training_mask
    mask = mask.unsqueeze(1)
    samples_count = len(mask.nonzero())

    cls_loss = torch.nn.functional.binary_cross_entropy(input=y_pred_cls, target=y_true_cls, weight=None, reduction='none')
    cls_loss = cls_loss * mask
    cls_loss = cls_loss.sum() / samples_count  # TODO should I divide by samples_count or len(det_mask.nonzero())?

    d1_gt, d2_gt, d3_gt, d4_gt = torch.split(y_true_geo, 1, 1)
    d1_pred, d2_pred, d3_pred, d4_pred = torch.split(y_pred_geo, 1, 1)
    area_gt = (d1_gt + d3_gt) * (d2_gt + d4_gt)
    area_pred = (d1_pred + d3_pred) * (d2_pred + d4_pred)
    w_intersect = torch.min(d2_gt, d2_pred) + torch.min(d4_gt, d4_pred)  # w or h, who cares
    h_intersect = torch.min(d1_gt, d1_pred) + torch.min(d3_gt, d3_pred)
    area_intersect = w_intersect * h_intersect
    area_union = area_gt + area_pred - area_intersect
    tensor_loss = area_intersect / area_union + 10 * (1 - torch.cos(theta_pred - theta_gt))
    det_mask = y_true_cls * mask
    tensor_loss = tensor_loss * det_mask
    reg_loss = tensor_loss.sum() / len(det_mask.nonzero())
    return cls_loss + reg_loss


def fit(start_epoch, model, loss_func, opt, lr_scheduler, best_score, max_batches_per_iter_cnt, checkpoint_dir, train_dl, valid_dl):
    batch_per_iter_cnt = 0
    for epoch in range(start_epoch, 9999999):
        model.train()
        train_loss_stats = 0.0
        loss_count_stats = 0
        pbar = tqdm.tqdm(train_dl, 'Epoch ' + str(epoch), ncols=120)
        for cropped, classification, regression, thetas, training_mask, file_names in pbar:
            if batch_per_iter_cnt == 0:
                optimizer.zero_grad()
            prediction = model(cropped.to('cuda'))
            loss = loss_func(prediction, (classification, regression, thetas, training_mask, file_names))
            train_loss_stats += loss.item()
            loss_count_stats += len(cropped)
            loss /= max_batches_per_iter_cnt
            loss.backward()
            batch_per_iter_cnt += 1
            if batch_per_iter_cnt == max_batches_per_iter_cnt:
                opt.step()
                batch_per_iter_cnt = 0
                pbar.set_postfix({'Mean loss over the epoch': train_loss_stats / loss_count_stats}, refresh=False)
        lr_scheduler.step(train_loss_stats / loss_count_stats, epoch)

        if valid_dl is None:
            val_loss = train_loss_stats / loss_count_stats
        else:
            model.eval()
            with torch.no_grad():
                val_loss = 0.0
                val_loss_count = 0
                for cropped, classification, regression, thetas, training_mask, file_names in valid_dl:
                    prediction = model(cropped.to('cuda'))
                    loss = loss_func(prediction, (classification, regression, thetas, training_mask, file_names))
                    val_loss += loss.item()
                    val_loss_count += len(cropped)
            val_loss /= val_loss_count
        print('Val loss: ', val_loss)

        if best_score > val_loss:
            best_score = val_loss
            save_as_best = True
        else:
            save_as_best = False
        save_checkpoint(epoch, model, opt, lr_scheduler, best_score, checkpoint_dir, save_as_best)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train-folder', type=str, required=True, help='Path to folder with train images and labels')
    parser.add_argument('--batch-size', type=int, default=8, help='Number of batches to process before train step')
    parser.add_argument('--batches-before-train', type=int, default=4, help='Number of batches to process before train step')
    parser.add_argument('--num-workers', type=int, default=8, help='Path to folder with train images and labels')
    args = parser.parse_args()

    icdar = datasets.ICDAR2015(args.train_folder, True, datasets.transform)
    dl = torch.utils.data.DataLoader(icdar, batch_size=args.batch_size, shuffle=True, sampler=None, batch_sampler=None, num_workers=args.num_workers)
    checkoint_dir = 'runs'
    epoch, model, optimizer, lr_scheduler, best_score = restore_checkpoint(checkoint_dir)
    fit(epoch, model, detection_loss, optimizer, lr_scheduler, best_score, args.batches_before_train, checkoint_dir, dl, None)
