import os
import _pickle as pickle
from e2efold_rfam.common.config import process_config
from e2efold_rfam.common.utils import get_args

args = get_args()

config_file = args.config

config = process_config(config_file)
print("#####Stage 1#####")
print('Here is the configuration of this run: ')
print(config)
os.environ["CUDA_VISIBLE_DEVICES"]= config.gpu #0,1,2,3,4

#import torch
#torch.multiprocessing.set_sharing_strategy('file_system')
import torch.optim as optim
from torch.utils import data
from e2efold_rfam.models import ContactNetwork, ContactNetwork_test, ContactNetwork_fc
from e2efold_rfam.models import ContactAttention, ContactAttention_simple_fix_PE
from e2efold_rfam.models import ContactAttention_simple
from e2efold_rfam.common.utils import *
from e2efold_rfam.postprocess import postprocess

conserved=config.conserved
d = config.u_net_d #10
BATCH_SIZE = config.batch_size_stage_1 #20
OUT_STEP = config.OUT_STEP #100
LOAD_MODEL = config.LOAD_MODEL #true
pp_steps = config.pp_steps #20
data_type = config.data_type #"rfam_all_600"
model_type = config.model_type #"att_simple_fix"
#supervised_att_simple_fix_rfam_all_600_d10_l3.pt
model_path = '../models_ckpt/supervised_{}_{}_d{}_l3.pt'.format(model_type, data_type,d)
epoches_first = config.epoches_first #50
evaluate_epi = config.evaluate_epi_stage_1 #5


steps_done = 0
# if gpu is to be used
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

seed_torch()

# for loading data
# loading the rna ss data, the data has been preprocessed
# 5s data is just a demo data, which do not have pseudoknot, will generate another data having that
from e2efold_rfam.data_generator import RNASSDataGenerator, Dataset
#import collections
#RNA_SS_data = collections.namedtuple('RNA_SS_data', 'seq ss_label length name pairs')

train_data = RNASSDataGenerator('../data/{}/'.format(data_type), 'train_0_1')
val_data = RNASSDataGenerator('../data/{}/'.format(data_type), 'val_0_1')
test_data = RNASSDataGenerator('../data/{}/'.format(data_type), 'test_0_1')


seq_len = train_data.data_y.shape[-2]
print('Max seq length ', seq_len)


# using the pytorch interface to parallel the data generation and model training
params = {'batch_size': BATCH_SIZE,
          'shuffle': True,
          'num_workers': 6,
          'drop_last': True}
train_set = Dataset(train_data)
train_generator = data.DataLoader(train_set, **params)

val_set = Dataset(val_data)
val_generator = data.DataLoader(val_set, **params)

params = {'batch_size': BATCH_SIZE,
          'shuffle': False,
          'num_workers': 6,
          'drop_last': False}
test_set = Dataset(test_data)
test_generator = data.DataLoader(test_set, **params)



# store the intermidiate activation

activation = {}
def get_activation(name):
    def hook(model, input, output):
        activation[name] = output.detach()
    return hook

if model_type =='test_lc':
    contact_net = ContactNetwork_test(d=d, L=seq_len).to(device)
if model_type == 'att6':
    contact_net = ContactAttention(d=d, L=seq_len).to(device)
if model_type == 'att_simple':
    contact_net = ContactAttention_simple(d=d, L=seq_len).to(device)
if model_type == 'att_simple_fix':
    contact_net = ContactAttention_simple_fix_PE(d=d, L=seq_len).to(device)
if model_type == 'fc':
    contact_net = ContactNetwork_fc(d=d, L=seq_len).to(device)
if model_type == 'conv2d_fc':
    contact_net = ContactNetwork(d=d, L=seq_len).to(device)

# contact_net.conv1d2.register_forward_hook(get_activation('conv1d2'))

#if LOAD_MODEL and os.path.isfile(model_path):
#    print('Loading u net model...')
#    contact_net.load_state_dict(torch.load(model_path,map_location=device))




# for 5s
# pos_weight = torch.Tensor([100]).to(device)
# for length as 600
pos_weight = torch.Tensor([300]).to(device)
criterion_bce_weighted = torch.nn.BCEWithLogitsLoss(pos_weight = pos_weight) #a weight of positive examples. Must be a vector with length equal to the number of classes.

# randomly select one sample from the test set and perform the evaluation
def model_eval():
    f1_no_train=list()
    contact_net.eval()
    for contacts, seq_embeddings, matrix_reps, seq_lens, conservations in val_generator: # pair,seq,zeros,length
        #contacts, seq_embeddings, matrix_reps, seq_lens = next(iter(val_generator))
        conservation_batch=torch.Tensor(conservations.float()).to(device)
        contacts_batch = torch.Tensor(contacts.float()).to(device) #NLL
        seq_embedding_batch = torch.Tensor(seq_embeddings.float()).to(device)
        matrix_reps_batch = torch.unsqueeze(torch.Tensor(matrix_reps.float()).to(device), -1)

        # padding the states for supervised training with all 0s
        state_pad = torch.zeros([matrix_reps_batch.shape[0],seq_len, seq_len]).to(device)
        PE_batch = get_pe(seq_lens, seq_len).float().to(device)

        with torch.no_grad():
            pred_contacts = contact_net(PE_batch,seq_embedding_batch, state_pad)

    #50 iterations for post-processing network
        u_no_train = postprocess(pred_contacts,seq_embedding_batch, 0.01, 0.1, 50, 1.0, True) #NLL

        map_no_train = (u_no_train > 0.5).float() #NLL

        f1_no_train_tmp = list(map(lambda i: F1_low_tri(map_no_train.cpu()[i],contacts_batch.cpu()[i]), range(contacts_batch.shape[0]))) #a list contains N elements
        f1_no_train+=f1_no_train_tmp

    print('Average val F1 score with pure post-processing: ', np.average(f1_no_train))

def model_eval_all_test():
    contact_net.eval()
    result_no_train = list()
    result_no_train_shift = list()
    seq_lens_list = list()
    batch_n = 0
    for contacts, seq_embeddings, matrix_reps, seq_lens, conservations in test_generator: # pair,seq,zeros,length
        #if batch_n%10==0:
            #print('Batch number: ', batch_n)
        batch_n += 1
        contacts_batch = torch.Tensor(contacts.float()).to(device)
        seq_embedding_batch = torch.Tensor(seq_embeddings.float()).to(device)
        matrix_reps_batch = torch.unsqueeze(torch.Tensor(matrix_reps.float()).to(device), -1)

        state_pad = torch.zeros([matrix_reps_batch.shape[0],seq_len, seq_len]).to(device)

        PE_batch = get_pe(seq_lens, seq_len).float().to(device)

        with torch.no_grad():
            pred_contacts = contact_net(PE_batch,seq_embedding_batch, state_pad)

        # only post-processing without learning
        u_no_train = postprocess(pred_contacts,seq_embedding_batch, 0.01, 0.1, 50, 1.0, True)

        map_no_train = (u_no_train > 0.5).float()

        result_no_train_tmp = list(map(lambda i: evaluate_exact(map_no_train.cpu()[i],
            contacts_batch.cpu()[i]), range(contacts_batch.shape[0])))
        result_no_train += result_no_train_tmp
        result_no_train_tmp_shift = list(map(lambda i: evaluate_shifted(map_no_train.cpu()[i],
            contacts_batch.cpu()[i]), range(contacts_batch.shape[0])))
        result_no_train_shift += result_no_train_tmp_shift
        seq_lens_list += list(seq_lens)

    nt_exact_p,nt_exact_r,nt_exact_f1 = zip(*result_no_train)
    nt_shift_p,nt_shift_r,nt_shift_f1 = zip(*result_no_train_shift)

    nt_exact_p = np.nan_to_num(np.array(nt_exact_p))
    nt_exact_r = np.nan_to_num(np.array(nt_exact_r))
    nt_exact_f1 = np.nan_to_num(np.array(nt_exact_f1))

    nt_shift_p = np.nan_to_num(np.array(nt_shift_p))
    nt_shift_r = np.nan_to_num(np.array(nt_shift_r))
    nt_shift_f1 = np.nan_to_num(np.array(nt_shift_f1))

    print('Average testing F1 score with pure post-processing: ', np.average(nt_exact_f1))

    print('Average testing F1 score with pure post-processing allow shift: ', np.average(nt_shift_f1))

    print('Average testing precision with pure post-processing: ', np.average(nt_exact_p))

    print('Average testing precision with pure post-processing allow shift: ', np.average(nt_shift_p))

    print('Average testing recall with pure post-processing: ', np.average(nt_exact_r))

    print('Average testing recall with pure post-processing allow shift: ', np.average(nt_shift_r))

#use multiple GPUs
contact_net = torch.nn.DataParallel(contact_net)
u_optimizer = optim.Adam(contact_net.parameters())

for epoch in range(epoches_first): #80 epoches

    # num_batches = int(np.ceil(train_data.len / BATCH_SIZE))

    for contacts, seq_embeddings, matrix_reps, seq_lens, conservations in train_generator:
        # contacts, seq_embeddings, matrix_reps, seq_lens = next(iter(train_generator))
        contact_net.train()
        contacts_batch = torch.Tensor(contacts.float()).to(device) #N*600*600
        seq_embedding_batch = torch.Tensor(seq_embeddings.float()).to(device) #N*600*4
        matrix_reps_batch = torch.unsqueeze(torch.Tensor(matrix_reps.float()).to(device), -1) #N*600*600*1 of all zeros

        # padding the states for supervised training with all 0s
        state_pad = torch.zeros([matrix_reps_batch.shape[0],seq_len, seq_len]).to(device) #N*600*600 of all zeros


        PE_batch = get_pe(seq_lens, seq_len).float().to(device) #N*L*111
        contact_masks = torch.Tensor(contact_map_masks(seq_lens, seq_len)).to(device) #N*L*L


        pred_contacts = contact_net(PE_batch, seq_embedding_batch, state_pad) #N*L*L

        # Compute loss
        loss_u = criterion_bce_weighted(pred_contacts*contact_masks, contacts_batch)

        # print(steps_done)
        if steps_done % OUT_STEP ==0:
            print('Stage 1, epoch: {},step: {}, loss: {}'.format(
                epoch, steps_done, loss_u))

        # Optimize the model
        u_optimizer.zero_grad()
        loss_u.backward()
        u_optimizer.step()
        steps_done=steps_done+1

    #evaluation_epi=5, use one bactch from evalution data every 5 epoches,totally 10 bacthes (epoch/5)
    #make sure there are more than epoch/5 batches in evalution data
    if epoch%evaluate_epi==0 and epoch!=0:
        model_eval()
        try:
            state_dict = contact_net.module.state_dict()
        except AttributeError:
            state_dict = contact_net.state_dict()
        torch.save(state_dict, model_path)

model_eval_all_test()
