import cv2
import warnings
warnings.simplefilter("ignore")
import torch.optim
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.functional import sigmoid
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
import torchvision.models as models
import numpy as np
import time
import argparse
import datetime
import os, sys
import matplotlib.pyplot as plt
from tqdm import tqdm
import numpy as np
from sklearn.metrics import roc_curve, auc
from networks import *
from dataset import *
from HistogramEqualize import *
from MedianBlur import *
from FrameCrop import *

##REPRODUCIBILITY
seed=42
torch.manual_seed(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
np.random.seed(seed)

parser = argparse.ArgumentParser(description='ce7454')
parser.add_argument('--model', type=str, default="resnet18", help='model name')
parser.add_argument('--bs', type=int, default=16, help='input batch size - default:128')
parser.add_argument('--epoch', type=int, default=5, help='number of epochs - default:10')
parser.add_argument('--lr', type=float, default=0.0001, help='initial learning rate - default:0.005')
parser.add_argument('--input_size', type=int, default=320, help='input size of the depth image - default:96')
parser.add_argument('--augment_probability', type=float, default=1.0, help='augment probability - default:1.0')
parser.add_argument('--momentum', type=float, default=0.9, help='momentum - default:0.9')
parser.add_argument('--weight_decay', type=float, default=0.0005, help='weight_decay - default:0.0005')
parser.add_argument('--checkpoint', type=str, default=None, help='path/to/checkpoint.pth.tar - default:None')
parser.add_argument('--print_interval', type=int, default=500, help='print interval - default:500')
parser.add_argument('--save_dir', type=str, default="experiments/", help='path/to/save_dir - default:experiments/')
parser.add_argument('--name', type=str, default=None, help='name of the experiment. It decides where to store samples and models. if none, it will be saved as the date and time')
parser.add_argument('--finetune', action='store_true', help='use a pretrained checkpoint - default:false')

parser.add_argument('--view', type=str, default='both', help='dataset view - frontal, lateral, both(default)')
parser.add_argument('--num_classes', type=int, default=5, help='number of classes - default:5')
parser.add_argument('--intermediate_size', type=int, default=5, help='anytime_prediction intermediate size')
parser.add_argument('--use_weighted_bce', action='store_true', help='weighted bceloss')
parser.add_argument('--unmentioned_w', type=float, default=0.3, help='unmentioned_w')
parser.add_argument('--uncertainty_w', type=float, default=0.5, help='uncertainty_w')


class_names = ['No Finding',
       'Enlarged Cardiomediastinum', 'Cardiomegaly', 'Lung Opacity',
       'Lung Lesion', 'Edema', 'Consolidation', 'Pneumonia', 'Atelectasis',
       'Pneumothorax', 'Pleural Effusion', 'Pleural Other', 'Fracture',
       'Support Devices']
global best_auc_val
best_auc_val = {'Atelectasis':[-9.99,-9.99], 'Cardiomegaly':[-9.99,-9.99], 'Consolidation':[-9.99,-9.99], 'Edema':[-9.99,-9.99], 'Pleural Effusion':[-9.99,-9.99]}

def print_options(opt):
    message = ''
    message += '----------------- Options ---------------\n'
    for k, v in sorted(vars(opt).items()):
        comment = ''
        default = parser.get_default(k)
        if v != default:
            comment = '\t[default: %s]' % str(default)
        message += '{:>25}: {:<30}{}\n'.format(str(k), str(v), comment)
    message += '----------------- End -------------------'
    print(message)
    # save to the disk
    expr_dir = os.path.join(opt.save_dir, opt.name)
    mkdirs(expr_dir)
    file_name = os.path.join(expr_dir, 'opt.txt')
    with open(file_name, 'wt') as opt_file:
        opt_file.write(message)
        opt_file.write('\n')

def mkdirs(paths):
    if isinstance(paths, list) and not isinstance(paths, str):
        for path in paths:
            mkdir(path)
    else:
        mkdir(paths)


def mkdir(path):
    if not os.path.exists(path):
        os.makedirs(path)

#using adam
# def adjust_learning_rate(optimizer, epoch, args):
#     """Sets the learning rate to the initial LR decayed by 10 every args.lr_decay epochs"""
#     # lr = 0.00005
#     lr = args.lr * (0.1 ** (epoch // args.lr_decay))
#     # print("LR is " + str(lr)+ " at epoch "+ str(epoch))
#     for param_group in optimizer.param_groups:
#         param_group['lr'] = lr
#     return optimizer

def set_default_args(args):
    if not args.name:
        now = datetime.datetime.now()
        args.name = now.strftime("%Y-%m-%d-%H-%M")
    args.is_cuda = torch.cuda.is_available()
    args.expr_dir = os.path.join(args.save_dir, args.name)

def cal_multilabel_accuracy(logits, targets, thres=0.5):
    #returns list of number of correct predictions for each class, total correct, and total number of samples
    batch_size, num_class = logits.shape
    total_correct = torch.tensor([0.0 for i in range(num_class)]).long()
    
    logits, targets = logits.cpu(), targets.cpu()
    probs = sigmoid(logits)
    pred = (probs>0.5).long()
    correct = (pred.long()==targets.long()).long()
    for batch in correct:
        total_correct += batch
    
    return total_correct, torch.sum(total_correct), batch_size

def compute_auc(all_logits, all_labels, num_classes=14):
    fpr = dict()
    tpr = dict()
    roc_auc = dict()
    assert(num_classes == all_labels.shape[-1])
    if num_classes == 5:
        global class_names
        if len(class_names) !=5:
            class_names = [class_names[i] for i in [8,2,6,5,10]]
    for i, c in enumerate(class_names):
        fpr[c], tpr[c], _ = roc_curve(all_labels[:, i], all_logits[:, i], pos_label=1)
        roc_auc[c] = auc(fpr[c], tpr[c])
        
    # Compute micro-average ROC curve and ROC area
    fpr["micro"], tpr["micro"], _ = roc_curve(all_labels.ravel(), all_logits.ravel())
    roc_auc["micro"] = auc(fpr["micro"], tpr["micro"])
    # atelectasis - 8, cardiomegaly - 2, consolidation - 6, edema - 5, pleural effusion - 10 
    if args.num_classes==14:   
        fpr["micro_5"], tpr["micro_5"], _ = roc_curve(all_labels[:,[8,2,6,5,10]].ravel(), all_logits[:,[8,2,6,5,10]].ravel())
        roc_auc["micro_5"] = auc(fpr["micro_5"], tpr["micro_5"])
    
    #prettify
    for key in roc_auc.keys():
        roc_auc[key] = round(roc_auc[key], 3)
    return roc_auc
    


def main(args):
    set_default_args(args)
    
    # ADD YOUR MODEL NAME HERE
    if args.model == 'resnet18':
        model = modified_resnet18(num_classes=args.num_classes)
    elif args.model == 'resnet152':
        model = modified_resnet152(num_classes=args.num_classes)
    elif args.model == 'densenet121':
        model = modified_densenet121(num_classes=args.num_classes)
    elif args.model == 'densenet201':
        model = modified_densenet201(num_classes=args.num_classes)
    elif args.model == 'squeezenet':
        model = modified_squeezenet(num_classes=args.num_classes)
    elif args.model == 'layer_sharing_resnet':
        model = layer_sharing_resnet(num_classes=args.num_classes)
    elif args.model == 'ensembling_network':
        model = ensembling_network(num_classes=args.num_classes)
    elif args.model == 'masked_duo_model':
        frontal_path = 'experiments/resnet18_frontal_5_equalized/model_best.pth.tar'
        lateral_path = 'experiments/resnet18_lateral_5_equalized/model_best.pth.tar'
        frontal_model = modified_resnet18(num_classes=args.num_classes)
        frontal_model, _, _ = load_checkpoint(frontal_path , frontal_model,'foo')
        lateral_model = modified_resnet18(num_classes=args.num_classes)
        lateral_model, _, _ = load_checkpoint(lateral_path, lateral_model, 'foo')
        model = masked_duo_model(frontal_model, lateral_model)
    elif args.model == 'masked_duo_model_freeze':
        frontal_path = 'experiments/resnet18_frontal_5_equalized/model_best.pth.tar'
        lateral_path = 'experiments/resnet18_lateral_5_equalized/model_best.pth.tar'
        frontal_model = modified_resnet18(num_classes=args.num_classes)
        frontal_model, _, _ = load_checkpoint(frontal_path , frontal_model,'foo')
        for param in frontal_model.parameters():
            param.requires_grad = False
        lateral_model = modified_resnet18(num_classes=args.num_classes)
        lateral_model, _, _ = load_checkpoint(lateral_path, lateral_model, 'foo')
        for param in lateral_model.parameters():
            param.requires_grad = False
        model = masked_duo_model(frontal_model, lateral_model)
    elif args.model == 'anytime_prediction_model_resnet18':
        model = anytime_prediction_model_resnet18(num_classes=args.num_classes, intermediate_size=args.intermediate_size)
    elif args.model == 'anytime_prediction_model_densenet121':
        model = anytime_prediction_model_densenet121(num_classes=args.num_classes, intermediate_size=args.intermediate_size)
    elif args.model == 'final_prediction_model':
        model1_path = 'experiments/resnet18_both_5_equalized/model_best.pth.tar'
        model2_path = 'experiments/resnet152_both_5/model_best.pth.tar'
        model3_path = 'experiments/densenet121_both_5/model_best.pth.tar'
        model1 = modified_resnet18(num_classes=args.num_classes)
        model1, _, _ = load_checkpoint(model1_path , model1,'foo')
        for param in model1.parameters():
            param.requires_grad = False
        model2 = modified_resnet152(num_classes=args.num_classes)
        model2, _, _ = load_checkpoint(model2_path, model2, 'foo')
        for param in model2.parameters():
            param.requires_grad = False
        model3 = modified_densenet121(num_classes=args.num_classes)
        model3, _, _ = load_checkpoint(model3_path, model3, 'foo')
        for param in model3.parameters():
            param.requires_grad = False
        model = final_prediction_model(model1, model2, model3)
    else:
        print(f'~~~ {args.model} not found! ~~~')


    model.float()
    if args.is_cuda: model.cuda()
    #TODO: use xavier if not pretrained
    #model.apply(weights_init)
    cudnn.benchmark = True
    criterion = nn.BCEWithLogitsLoss()
    
    # mean=127.898, std=74.69748171138374
    xforms_train = transforms.Compose([FrameCrop(60, 20),
                                       transforms.Resize(365),
                                       transforms.RandomCrop(args.input_size),
                                       HistogramEqualize(),
                                       MedianBlur(3)])
    xforms_val = transforms.Compose([FrameCrop(60, 20),
                                     transforms.Resize(365),
                                     transforms.CenterCrop(args.input_size),
                                     HistogramEqualize(),
                                     MedianBlur(3)])
    
    
    train_dataset = CheXpertDataset(training = True,transform=xforms_train, view=args.view, num_classes=args.num_classes)
    valid_dataset = CheXpertDataset(training = False,transform=xforms_val, view=args.view,num_classes=args.num_classes)
    if args.model == 'masked_duo_model' or args.model == 'masked_duo_model_freeze':
        train_dataset = CheXpertDataset_paired(training = True,transform=xforms_train, num_classes=args.num_classes)
        valid_dataset = CheXpertDataset_paired(training = False,transform=xforms_val, num_classes=args.num_classes)
    if args.use_weighted_bce:
        train_dataset = CheXpertDataset_weights(training = True,transform=xforms_train, num_classes=args.num_classes,unmentioned_w=args.unmentioned_w, uncertainty_w=args.uncertainty_w)
        valid_dataset = CheXpertDataset_weights(training = False,transform=xforms_val, num_classes=args.num_classes,unmentioned_w=args.unmentioned_w, uncertainty_w=args.uncertainty_w)
    optimizer = torch.optim.Adam(model.parameters(), args.lr)

                
    train_loader = torch.utils.data.DataLoader(
       train_dataset, batch_size=args.bs, shuffle = True,
       num_workers=8, pin_memory=False)

    val_loader = torch.utils.data.DataLoader(
       valid_dataset, batch_size=args.bs  ,shuffle = True,
       num_workers=8, pin_memory=False)
    current_epoch = 0
    if args.checkpoint:
        model, optimizer, current_epoch = load_checkpoint(args.checkpoint, model, optimizer)
        if args.finetune:
            current_epoch = 0

    best = False

    print_options(args)
    training_loss, val_loss, time_taken, train_avg_acc_list, train_individual_acc_list, test_avg_acc_list, test_individual_acc_list  =  ([] for i in range(7))
    best = False
    thres = 0

    for epoch in tqdm(range(current_epoch, args.epoch), desc='Epoch'):

        # train for one epoch
        epoch_train_loss, TT, train_avg_acc, train_individual_acc = train(train_loader, model, criterion, optimizer, epoch, args)
        training_loss = training_loss + [epoch_train_loss]
        train_avg_acc_list += [train_avg_acc]
        train_individual_acc_list += [train_individual_acc]
        
        # evaluate on validation set
        loss_val, test_avg_acc, test_individual_acc = validate(val_loader, model, criterion , args, epoch)
        val_loss = val_loss + [loss_val]
        test_avg_acc_list += [test_avg_acc]
        test_individual_acc_list += [test_individual_acc]

        state = {
            'epoch': epoch,
            'arch': args.model,
            'state_dict': model.state_dict(),
            # 'optimizer' : optimizer.state_dict(),
        }

        if not os.path.isfile(os.path.join(args.expr_dir, 'model_best.pth.tar')):
            save_checkpoint(state, True, args)

        if (epoch > 1) :
            best = (loss_val < min(val_loss[:len(val_loss)-1]))
            if best:
                tqdm.write("saving best performing checkpoint on val")
                save_checkpoint(state, True, args)

        save_checkpoint(state, False, args)
    #
    
    print('\nBEST AUC FOR 5 EVAL CLASSES [AUC, EPOCH]')
    print ('\t'.join([f'{key}:{best_auc_val[key]}'for key in best_auc_val]))
    
    save_plt([train_avg_acc_list, test_avg_acc_list], ["train_avg_acc", "test_avg_acc"], args)
    save_plt([training_loss, val_loss], ["train_loss", "val_loss"], args)
    save_plt([time_taken], ["time_taken"], args)

    
    
def train(train_loader, model, criterion, optimizer, epoch, args):


    all_logits = None
    all_labels = None
    correct = torch.tensor([0.0 for i in range(args.num_classes)]).long()
    total_correct = 0
    total_batches = 0
    running_loss = 0.0
    # switch to train mode
    model.train()
    stime = time.time()
    for i, (input, target) in enumerate(tqdm(train_loader, desc='Train iterations')):
        # measure data loading time
        target = target.float()
        input = input.float()
        if args.is_cuda:
            target = target.cuda()
            input = input.cuda()
        # compute output
        output = model(input)
        if args.use_weighted_bce:
            weights = target.clone()
            target[target==args.unmentioned_w] = 0.
            target[target==args.uncertainty_w] = 1.0
            loss = F.binary_cross_entropy_with_logits(output, target, weight=weights)
        else:    
            loss = criterion(output, target)
        running_loss += loss.item()
        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
        individual_correct, batch_correct, num_batch = cal_multilabel_accuracy(output,target)
        #add step results to epoch results
        correct += individual_correct
        total_correct += batch_correct
        total_batches += num_batch
        
        #for auc computation
        output = sigmoid(output)
        if all_logits is None:
            all_logits = output.detach().cpu().numpy()
        else:
            all_logits = np.concatenate((all_logits, output.detach().cpu().numpy()), axis=0)
            
        if all_labels is None:
            all_labels = target.detach().cpu().numpy()
        else:    
            all_labels = np.concatenate((all_labels, target.detach().cpu().numpy()), axis=0)

    TT = time.time() - stime
    running_loss =  running_loss/(i+1)
    avg_acc = 100*total_correct.float()/(total_batches*args.num_classes)
    if args.num_classes == 14:
        avg_acc_5 = 100*torch.sum(correct[[8,2,6,5,10]]).float()/(total_batches*5)
    else:
        avg_acc_5 = 0.0
    individual_acc = 100*correct.float()/total_batches
    
    auc = compute_auc(all_logits, all_labels, num_classes=args.num_classes)

    message = f"\n\n\n======= Epoch [{epoch}] ======= \n== Training Performance: ==\n"
    
    for i, c in enumerate(class_names):
        acc = individual_acc[i]
        message += f"{c}: {acc:.05}%\t"
    tqdm.write(message)
    
    tqdm.write('Training Loss {loss:.4f}\t'
          'Average Acc: {avg_acc:.3f}\t'
          'Average Acc for 5 labels: {avg_acc_5:.3f}'
          '\nAUC: {auc}\t'.format(loss=running_loss, avg_acc=avg_acc, avg_acc_5=avg_acc_5, auc= '\n'.join([f'{key}:{auc[key]}'for key in auc])))

    return running_loss, TT, avg_acc, individual_acc



def validate(val_loader, model, criterion, args, epoch):
    
    all_logits = None
    all_labels = None
    correct = torch.tensor([0.0 for i in range(args.num_classes)]).long()
    total_correct = 0
    total_batches = 0
    correct = 0
    running_loss = 0.0
    
    model.eval()
    with torch.no_grad():

        for i, (input, target) in enumerate(tqdm(val_loader, desc='Val iterations')):

            target = target.float()
            input = input.float()
            if args.is_cuda:
                target = target.cuda()
                input = input.cuda()
            output = model(input)
            if args.use_weighted_bce:
                weights = target.clone()
                target[target==args.unmentioned_w] = 0.
                target[target==args.uncertainty_w] = 1.0
                loss = F.binary_cross_entropy_with_logits(output, target, weight=weights)
            else:    
                loss = criterion(output, target)
            running_loss += loss.item()
            individual_correct, batch_correct, num_batch =cal_multilabel_accuracy(output,target)
            correct += individual_correct
            total_correct += batch_correct
            total_batches += num_batch
            
            #for auc computation
            output = sigmoid(output)
            if all_logits is None:
                all_logits = output.detach().cpu().numpy()
            else:
                all_logits = np.concatenate((all_logits, output.detach().cpu().numpy()), axis=0)
                
            if all_labels is None:
                all_labels = target.detach().cpu().numpy()
            else:    
                all_labels = np.concatenate((all_labels, target.detach().cpu().numpy()), axis=0)

    running_loss =  running_loss/(i+1)
    avg_acc = 100*total_correct.float()/(total_batches *args.num_classes)
    if args.num_classes == 14:
        avg_acc_5 = 100*torch.sum(correct[[8,2,6,5,10]]).float()/(total_batches*5)
    else:
        avg_acc_5 = 0
    individual_acc = 100*correct.float()/total_batches
    
    auc = compute_auc(all_logits, all_labels,num_classes=args.num_classes)
    
    message = '\n\n== Validation Performance: ==\n'
    for i, c in enumerate(class_names):
        acc = individual_acc[i]
        message += f"{c}: {acc:.05}%\t"
    tqdm.write(message)
    
    tqdm.write('Loss {loss:.4f}\t'
          'Average Acc: {avg_acc:.3f}\t'
          'Average Acc for 5 labels: {avg_acc_5:.3f}'
          '\nAUC: {auc}\t'.format(loss=running_loss, avg_acc=avg_acc, avg_acc_5=avg_acc_5, auc='\n'.join([f'{key}:{auc[key]}'for key in auc])))
    

    for key in best_auc_val.keys():
        if auc[key] > best_auc_val[key][0]:
            best_auc_val[key] = [auc[key], epoch]
    
    return running_loss, avg_acc, individual_acc


def weights_init(m):
    if isinstance(m, nn.Conv2d):
        nn.init.xavier_uniform_(m.weight.data)
        # nn.init.xavier_uniform_(m.bias.data)

def save_checkpoint(state, is_best, opt, filename='checkpoint.pth.tar'):
    expr_dir = os.path.join(opt.save_dir, opt.name)
    torch.save(state, os.path.join(expr_dir, filename))
    if is_best:
        torch.save(state, os.path.join(expr_dir, 'model_best.pth.tar'))

def load_checkpoint(path, model, optimizer):
    checkpoint = torch.load(path)
    model.load_state_dict(checkpoint['state_dict'])
    # optimizer.load_state_dict(checkpoint['optimizer'])
    epoch  = checkpoint['epoch']

    return model, optimizer, epoch

def save_plt(array, name, args):
    colors = ['blue','red','green','pink','purple']
    plt.cla()
    plt.clf()
    plt.close()
    for i in range(len(array)):
        np.savetxt(os.path.join(args.expr_dir,name[i]+'.txt'), array[i], fmt='%f')
        plt.plot(array[i],color=colors[i], label=name[i])
        plt.xlabel('epoch')
        plt.legend()
    plt.savefig(os.path.join(args.expr_dir, name[i]+'.png'))
    plt.cla()
    plt.clf()
    plt.close()

if __name__ == '__main__':
    args = parser.parse_args()
    main(args)