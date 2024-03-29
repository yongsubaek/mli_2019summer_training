### IMPORT MODULES
# basic
import numpy as np
# torch
import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
# text tools
from torchtext.data import Field, BucketIterator
from torchtext.datasets import Multi30k
from nltk.translate.bleu_score import sentence_bleu,SmoothingFunction
# utils
import random, time, spacy, os
from argparse import ArgumentParser
import matplotlib.pyplot as plt
# multi gpu
try:
    from apex.parallel import DistributedDataParallel as DDP
except ImportError:
    print("Multi GPU is not available\nPlease install apex from https://www.github.com/nvidia/apex")
# from other codes
from beam_search import *
from plot import plot_and_save
### Utils
tt = time.time
def tokenize(text):
    return [tok.text for tok in spacy_en.tokenizer(text)]
def tokenize_reverse(text):
    return [tok.text for tok in spacy_de.tokenizer(text)][::-1]
def init_weights(m):
    if (type(m) == nn.LSTM) or (type(m) == nn.GRU):
        for name, param in m.named_parameters():
            nn.init.uniform_(param.data, -0.08, 0.08)
def detokenize(index, vocab):
    if vocab.itos[index] in ["<eos>", "<sos>", "<pad>"]:
        return ""
    return vocab.itos[index]

### Seq2Seq Model
class Encoder(nn.Module):
    """
    Encode sentences to context vectors
    """
    def __init__(self, input_dim, emd_dim, hidden_dim, num_layers, dropout=0.5, rnn_type="LSTM", bidirectional=False):
        super(Encoder, self).__init__()
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers

        self.num_directions = 2 if bidirectional else 1
        # Layers
        self.emb = nn.Embedding(input_dim, emd_dim)
        self.dropout = nn.Dropout(dropout)
        if rnn_type == "LSTM":
            self.rnn = nn.LSTM(emd_dim, hidden_dim, num_layers, bidirectional=bidirectional)
        else:
            self.rnn = nn.GRU(emd_dim, hidden_dim, num_layers, bidirectional=bidirectional)

    def forward(self, input):
        """
        input: (batch of) input sentence tensors
        output: (batch of) context vectors
        """
        output = self.emb(input)
        output = self.dropout(output)
        output, hidden = self.rnn(output)
        return hidden # Context Vector

class Decoder(nn.Module):
    """
    Decode the context vector ONE STEP
    """
    def __init__(self, hidden_dim, output_dim, num_layers, dropout=0.5, rnn_type="LSTM", bidirectional=False):
        super(Decoder, self).__init__()
        self.hidden_dim = hidden_dim
        self.output_dim = output_dim
        self.num_layers = num_layers
        self.num_directions = 2 if bidirectional else 1
        # Layers
        self.emb = nn.Embedding(output_dim, emd_dim) # output_dim: vocabulary size of target
        self.dropout = nn.Dropout(dropout)
        if rnn_type == "LSTM":
            self.rnn = nn.LSTM(emd_dim, hidden_dim, num_layers, bidirectional=bidirectional)
        else:
            self.rnn = nn.GRU(emd_dim, hidden_dim, num_layers, bidirectional=bidirectional)
        self.fc = nn.Linear(hidden_dim * self.num_directions, output_dim)
        self.log_softmax = nn.LogSoftmax(dim=1)

    def forward(self, input, hidden):
        """
        input1: (batch of) words. size: [batch_size]
        input2: (batch of) hidden from last layer(decoder) or context vector from encoder. [num_layers*num_directions, batch_size, output_dim]
        output1: (batch of) translated words. size: [batch_size, output_dim]
        output2: same type of input2
        """
        # print("Input shape: {}\n".format(input.shape))
        output = self.emb(input).unsqueeze(0)
        output = self.dropout(output)
        # print("Embeded shape: {}\n".format(output.shape))
        # print("Hidden shape: {}\n".format(" and ".join([ str(x.shape) for x in hidden])))
        # output = F.relu(output)
        output, hidden = self.rnn(output, hidden)
        # print("After RNN shape: {}\n".format(output.shape))
        # output: [1, batch_size, hidden_dim]
        output = self.log_softmax(self.fc(output.squeeze(0)))
        return output, hidden

class Seq2Seq(nn.Module):
    """
    Combine Encoder and Decoder
    """
    def __init__(self, encoder, decoder):
        super(Seq2Seq, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
    def forward(self, source, target):
        """
        input: (batch of) pairs of source and target sequences. size: [sentence_length, batch_size]
        output: (batch of) translated sentences. size: (train)[max_sentence_length, batch_size, target_vocab_size], (test)[max_sentence_length, batch_size]
        """
        max_length = target.shape[0]
        batch_size = target.shape[1]

        hidden = self.encoder(source)
        if self.training:
            outputs = torch.ones(max_length, batch_size, self.decoder.output_dim, device=device) * target_field.vocab.stoi['<sos>']
            input = target[0,:] # a batch of <sos>'s'
            for i in range(1, max_length):
                # Teacher forcing
                output, hidden = self.decoder(input, hidden)
                outputs[i] = output
                input = target[i]
        else:
            # Beam Search: top 2
            # outputs = torch.zeros(max_length, batch_size, device=device)
            beam_width = 2
            n_sen = 1
            t1 = tt()
            decode_batch = beam_decode(self.decoder, target, hidden, beam_width, n_sen) # returns: python list of sentence(list of str). size: [batch_size, sentence_length]
            t2 = tt()
            # print("Beam Search: {:.3f} sec\n".format(t2-t1))
            outputs = decode_batch
            # for i in range(batch_size):
            #     if len(decode_batch[i]) < max_length:
            #         output = F.pad(torch.tensor(decode_batch[i], dtype=torch.int), (0, max_length - len(decode_batch[i])), 'constant', target_field.vocab.stoi['<eos>'])
            #     else:
            #         output = torch.tensor(decode_batch[i], dtype=torch.int)
            #     outputs[:,i] = output

        return outputs
### Train and Evaluation
def train(model, iterator, optimizer, criterion):
    model.train()
    epoch_loss = 0
    clip = 1
    for i, batch in enumerate(iterator):
        optimizer.zero_grad()

        src = batch.src
        trg = batch.trg

        output = model(src, trg)

        trg = trg[1:].view(-1)
        output = output[1:].view(-1, output.shape[-1])

        loss = criterion(output, trg)
        loss.backward()
        # clip
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()
        epoch_loss += loss.item()
        # break # for debugging
    return epoch_loss / len(iterator)

def evaluate(model, iterator, vocab, print_eg=False):
    #@TODO bleu score fix
    model.eval()
    # reference = [['the', 'quick', 'brown', 'fox', 'jumped', 'over', 'the', 'lazy', 'dog']]
    # candidate = ['the', 'quick', 'brown', 'fox', 'jumped', 'over', 'the']
    # score = sentence_bleu(reference, candidate) # -> 0.75xxx...
    bleu_score = 0.
    num_sen = 0
    sf = SmoothingFunction()
    # Collect test sentence
    # f = open("test_results.txt", "w")
    with torch.no_grad():
        for i, batch in enumerate(iterator):

            src = batch.src
            trg = batch.trg

            output = model(src, trg)

            trg = trg.transpose(0,1) # -> [batch_size, max_length]

            trg = [ list(filter(lambda a: a not in [""], [ detokenize(idx.item(), vocab) for idx in utt ] )) for utt in trg ]
            output = [ [ list(filter(lambda a: a not in [""], [ detokenize(idx, vocab) for idx in utt ])) for utt in hypos ] for hypos in output ]
            if print_eg:
                print("Target sentence: \"{}\"".format(" ".join(trg[0])))
                print("Hypothesis sentences: \"{}\"\n".format("\" , \"".join([ " ".join(sen) for sen in output[0]])))
                # for idx in range(len(trg)):
                #     f.write("Target sentence: \"{}\"\n".format(" ".join(trg[idx])))
                #     f.write("Hypothesis sentences: \"{}\"\n\n".format("\" , \"".join([ " ".join(sen) for sen in output[idx]])))

            for output_sens, trg_sen in zip(output, trg):
                bleu_score += sentence_bleu(output_sens, trg_sen, smoothing_function=sf.method7)
                num_sen += 1
    # f.close()
    return bleu_score * 100 / num_sen

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument('-seed', type=int, default=9)
    parser.add_argument('-b', "--batch_size", type=int, default=128, help='batch size(default=128)')
    parser.add_argument('-num-layers', type=int, default=4)
    parser.add_argument('-emd-dim', type=int, default=256)
    parser.add_argument('-hidden-dim', type=int, default=512)
    parser.add_argument('--no-reverse', help='not to reverse input seq', action='store_true')
    parser.add_argument('--bidirectional', help='bidirectional rnn', action='store_true')
    parser.add_argument('-lr', type=float, default=1e-3)
    parser.add_argument('-rnn-type', choices=['LSTM', 'GRU'], default="LSTM", help="LSTM or GRU")
    parser.add_argument('-opt', choices=['adam', 'sgd'], default='sgd')
    parser.add_argument('-epochs', type=int, default=10)
    parser.add_argument('-dropout', type=float, help='dropout rate', default=0.5)
    parser.add_argument('--cpu', help='forcing to use cpu', action='store_true')
    parser.add_argument('-resume', type=str, help='load model from checkpoint(input: path of ckpt)')
    parser.add_argument('--evaluate', help='Not train, Only evaluate', action='store_true')
    parser.add_argument('-v', '--verbose', help="0: nothing, 1: test only, else: eval and test", type=int, default=1)
    # multi gpu setting
    parser.add_argument("--local_rank", help="automatically selected by apex. do not set it manually.", default=0, type=int)
    parser.add_argument("--no-multi", help="use single gpu", action="store_true")
    global args
    args = parser.parse_args()
    # multi gpu
    args.distributed = False
    if not args.no_multi and 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        args.gpu = args.local_rank
        torch.cuda.set_device(args.gpu)
        torch.distributed.init_process_group(backend='nccl',
                                             init_method='env://')
        args.world_size = torch.distributed.get_world_size()

        assert torch.backends.cudnn.enabled, "Amp requires cudnn backend to be enabled."

    # global device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if args.cpu:
        device = torch.device("cpu")
    print("Using Device: {}".format(device))
    # Random Seed
    random_seed = args.seed
    random.seed(random_seed)
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)
    torch.backends.cudnn.deterministic = True

    # Hyperparameters loading
    epochs = args.epochs
    batch_size = args.batch_size
    rnn_type = args.rnn_type
    reverse = not args.no_reverse
    bidirectional = args.bidirectional
    num_layers = args.num_layers
    emd_dim = args.emd_dim
    hidden_dim = args.hidden_dim
    lr = args.lr
    do_train = not args.evaluate

    print("Options: {}\n".format(args))

    params = [batch_size, rnn_type, reverse, bidirectional, num_layers, emd_dim, hidden_dim,
                lr]
    if args.distributed:
        params.append("MultiGPU")

    model_name = "seq2seq-{}".format("-".join([ str(p) for p in params ]))
    PATH = os.path.join("models", model_name)


    # Preparing data
    t1 = tt()
    spacy_de = spacy.load('de')
    spacy_en = spacy.load('en')
    source_field = Field(sequential = True,
                        use_vocab = True,
                        tokenize = tokenize_reverse if reverse else tokenize,
                        init_token = '<sos>',
                        eos_token = '<eos>',
                        lower = True,
                        batch_first = False)
    target_field = Field(sequential = True,
                        use_vocab = True,
                        tokenize = tokenize,
                        init_token = '<sos>',
                        eos_token = '<eos>',
                        lower = True,
                        batch_first = False)
    train_data, valid_data, test_data = Multi30k.splits(exts = ('.de', '.en'),
                                                        fields = (source_field, target_field))
    source_field.build_vocab(train_data, min_freq = 2)
    target_field.build_vocab(train_data, min_freq = 2)

    train_iterator, valid_iterator, test_iterator = \
                        BucketIterator.splits((train_data, valid_data, test_data),
                                                batch_size = batch_size,
                                                device = device)
    t2 = tt()
    print("Data ready: {:.3f} sec\n".format(t2-t1))

    # model inputs
    input_dim = len(source_field.vocab)
    output_dim = len(target_field.vocab)

    encoder = Encoder(input_dim=input_dim, emd_dim=emd_dim, hidden_dim=hidden_dim,
                        num_layers=num_layers, rnn_type=rnn_type, bidirectional=bidirectional).to(device)
    decoder = Decoder(hidden_dim=hidden_dim, output_dim=output_dim,
                        num_layers=num_layers, rnn_type=rnn_type, bidirectional=bidirectional).to(device)
    model = Seq2Seq(encoder, decoder).to(device)
    model.apply(init_weights) # weight initialization
    # parallel
    if args.distributed:
        model = DDP(model, delay_allreduce=True)  # multi gpu

    optimizer = optim.SGD(model.parameters(), lr=lr) if args.opt == "sgd" else optim.Adam(model.parameters(), lr=lr)
    if str(device) == 'cuda':
        criterion = nn.NLLLoss(ignore_index=target_field.vocab.stoi['<pad>']).cuda()
    else:
        criterion = nn.NLLLoss(ignore_index=target_field.vocab.stoi['<pad>'])
    # Training
    if do_train:
        load_epoch = 0
        train_losses = []
        eval_scores = []
        best_eval_score = -float("inf")
        # Load Existing Model
        if args.resume:
            print("Existing Model Loaded")
            checkpoint = torch.load(args.resume)
            load_epoch = checkpoint['epoch']
            train_losses = checkpoint['losses']
            eval_scores = checkpoint['scores'] if 'scores' in checkpoint else []
            load_args = checkpoint['args']

            model = Seq2Seq(encoder, decoder).to(device)
            if args.distributed:
                model = DDP(model, delay_allreduce=True)  # multi gpu
            optimizer = optim.SGD(model.parameters(), lr=load_args.lr) if load_args.opt == "sgd" else optim.Adam(model.parameters(), lr=load_args.lr)
            model.load_state_dict(checkpoint['model_state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        # Training Main body
        model.train()
        print("Model Training Start\n")
        t1 = tt()
        epochs = load_epoch + epochs
        for epoch in range(load_epoch, epochs):
            tt1 = tt()
            # Train
            train_loss = train(model, train_iterator, optimizer, criterion)
            tt2 = tt()
            print("[{}/{}]Train time per epoch: {:.3f}".format(epoch+1, epochs, tt2-tt1))
            train_losses.append(train_loss)
            # Validation
            eval_score = evaluate(model, valid_iterator, target_field.vocab, args.verbose > 1)
            eval_scores.append(eval_score)
            tt3 = tt()
            print("[{}/{}]Eval time per epoch: {:.3f}".format(epoch+1, epochs, tt3-tt2))

            print("[{}/{}]Train loss: {:.4f}, BLEU score: {:.4f}\n".format(epoch+1, epochs, train_loss, eval_score))
            # Update the best model
            if eval_score >= best_eval_score:
                torch.save({
                        'epoch': epoch,
                        'model_state_dict': model.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                        'losses': train_losses,
                        'scores': eval_scores,
                        'args': args
                        }, PATH + "-{}.ckpt".format(epoch))
                best_eval_score = eval_score
                print("Best Model Updated\n")

        t2 = tt()
        print("Model Training ends ({:.3f} min)\n".format((t2-t1) / 60))
        # Save the final model
        torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'losses': train_losses,
                'scores': eval_scores,
                'args': args
                }, PATH + "-final.ckpt")
        print("Model Saved\n")
    # Evaluation on Test Dataset
    print("Model Evaluation on Test Dataset")
    et1 = tt()
    if args.evaluate and args.resume:
        checkpoint = torch.load(args.resume)
        load_args = checkpoint['args']

        model = Seq2Seq(encoder, decoder).to(device)
        if args.distributed:
            model = DDP(model, delay_allreduce=True) # multi gpu
        optimizer = optim.SGD(model.parameters(), lr=load_args.lr) if load_args.opt == "sgd" else optim.Adam(model.parameters(), lr=load_args.lr)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    model.eval()
    test_score = evaluate(model, test_iterator, target_field.vocab, args.verbose >= 1)
    print("Test BLEU score: {:.2f}".format(test_score))
    et2 = tt()
    print("Evaluation Time: {:.4f} sec".format(et2-et1))

    # plot loss and score and save plots
    save_path = model_name
    file_type = "pdf"
    plot_and_save(PATH + "-final.ckpt", save_path, file_type)
