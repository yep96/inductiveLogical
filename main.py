#!/usr/bin/python3
import argparse
import json
import logging
import os
from neighborGraph import neighborGraph

import numpy as np
import torch
from torch.utils.data import DataLoader
from models import KGReasoning
from dataloader import TestDataset, TrainDataset, SingledirectionalOneShotIterator
from tensorboardX import SummaryWriter
import pickle
from collections import defaultdict
from util import flatten_query, list2tuple, parse_time, set_global_seed, eval_tuple

from rule import GraphRule
from ruledata import Data

ours = 'ns'

query_name_dict = {('e',('r',)): '1p',
                    ('e', ('r', 'r')): '2p',
                    ('e', ('r', 'r', 'r')): '3p',
                    (('e', ('r',)), ('e', ('r',))): '2i',
                    (('e', ('r',)), ('e', ('r',)), ('e', ('r',))): '3i',
                    ((('e', ('r',)), ('e', ('r',))), ('r',)): 'ip',
                    (('e', ('r', 'r')), ('e', ('r',))): 'pi',
                    (('e', ('r',)), ('e', ('r', 'n'))): '2in',
                    (('e', ('r',)), ('e', ('r',)), ('e', ('r', 'n'))): '3in',
                    ((('e', ('r',)), ('e', ('r', 'n'))), ('r',)): 'inp',
                    (('e', ('r', 'r')), ('e', ('r', 'n'))): 'pin',
                    (('e', ('r', 'r', 'n')), ('e', ('r',))): 'pni',
                    (('e', ('r',)), ('e', ('r',)), ('u',)): '2u-DNF',
                    ((('e', ('r',)), ('e', ('r',)), ('u',)), ('r',)): 'up-DNF',
                    ((('e', ('r', 'n')), ('e', ('r', 'n'))), ('n',)): '2u-DM',
                    ((('e', ('r', 'n')), ('e', ('r', 'n'))), ('n', 'r')): 'up-DM'
                }
name_query_dict = {value: key for key, value in query_name_dict.items()}
all_tasks = list(name_query_dict.keys()) # ['1p', '2p', '3p', '2i', '3i', 'ip', 'pi', '2in', '3in', 'inp', 'pin', 'pni', '2u-DNF', '2u-DM', 'up-DNF', 'up-DM']

def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description='Training and Testing Knowledge Graph Embedding Models',
        usage='train.py [<args>] [-h | --help]'
    )

    parser.add_argument('--cuda', action='store_true', help='use GPU')

    parser.add_argument('--do_train', action='store_true', help="do train")
    parser.add_argument('--do_valid', action='store_true', help="do valid")
    parser.add_argument('--do_test', action='store_true', help="do test")

    parser.add_argument('--data_path', type=str, default=None, help="KG data path")
    parser.add_argument('-n', '--negative_sample_size', default=128, type=int, help="negative entities sampled per query")
    parser.add_argument('-d', '--hidden_dim', default=500, type=int, help="embedding dimension")
    parser.add_argument('-g', '--gamma', default=12.0, type=float, help="margin in the loss")
    parser.add_argument('-b', '--batch_size', default=1024, type=int, help="batch size of queries")
    parser.add_argument('--test_batch_size', default=1, type=int, help='valid/test batch size')
    parser.add_argument('-lr', '--learning_rate', default=0.0001, type=float)
    parser.add_argument('-cpu', '--cpu_num', default=10, type=int, help="used to speed up torch.dataloader")
    parser.add_argument('-save', '--save_path', default=None, type=str, help="no need to set manually, will configure automatically")
    parser.add_argument('--max_steps', default=1000000, type=int, help="maximum iterations to train")
    parser.add_argument('--warm_up_steps', default=None, type=int, help="no need to set manually, will configure automatically")

    parser.add_argument('--save_checkpoint_steps', default=1000, type=int, help="save checkpoints every xx steps")
    parser.add_argument('--valid_steps', default=10000, type=int, help="evaluate validation queries every xx steps")
    parser.add_argument('--log_steps', default=100, type=int, help='train log every xx steps')
    parser.add_argument('--test_log_steps', default=10000, type=int, help='valid/test log every xx steps')

    parser.add_argument('--nentity', type=int, default=0, help='DO NOT MANUALLY SET')
    parser.add_argument('--nrelation', type=int, default=0, help='DO NOT MANUALLY SET')

    parser.add_argument('--geo', default='vec', type=str, choices=['vec', 'box', 'beta', 'ns'], help='the reasoning model, vec for GQE, box for Query2box, beta for BetaE, ns for neural-symbolic')
    parser.add_argument('--print_on_screen', action='store_true')

    parser.add_argument('--tasks', default='1p.2p.3p.2i.3i.ip.pi.2in.3in.inp.pin.pni.2u.up', type=str, help="tasks connected by dot, refer to the BetaE paper for detailed meaning and structure of each task")
    parser.add_argument('--seed', default=0, type=int, help="random seed")
    parser.add_argument('-betam', '--beta_mode', default="(1600,2)", type=str, help='(hidden_dim,num_layer) for BetaE relational projection')
    parser.add_argument('-boxm', '--box_mode', default="(none,0.02)", type=str, help='(offset activation,center_reg) for Query2box, center_reg balances the in_box dist and out_box dist')
    parser.add_argument('-pretrain', '--KGE_pretrain', action='store_true', help="use the kg pretrain model")
    parser.add_argument('-kge', '--kge_mode', default="TransE", type=str, help='KG embedding used in \'ns\' way')
    parser.add_argument('-weight', '--loss_weight', default=0.1, type=int, help='the weight to balance the loss of the two parts of \'ns\'')
    parser.add_argument('--prefix', default=None, type=str, help='prefix of the log path')
    parser.add_argument('--checkpoint_path', default=None, type=str, help='path for loading the checkpoints')
    parser.add_argument('-evu', '--evaluate_union', default="DNF", type=str, choices=['DNF', 'DM'], help='the way to evaluate union queries, transform it to disjunctive normal form (DNF) or use the De Morgan\'s laws (DM)')
    parser.add_argument('-newloss', '--new_loss', action='store_true', help="use the v2b loss")

    parser.add_argument('-use_rule', action='store_true')
    parser.add_argument('-rule_len', type=int, required=False, help='the max length of rule')
    parser.add_argument('-rule_thr', type=float, required=False, help='the threhold of rule confidence')

    parser.add_argument('-pre_1p', default=False, action='store_true', help="pretrain 1p tasks")
    parser.add_argument('-gridsearch', default=False, action='store_true', help="find hyper parameter to use vec&emb")
    parser.add_argument('-lambdas', default='', type=str, help="hyper parameter to use vec&emb, use ';' to split")

    parser.add_argument('-max_n', '--max_neighbor', type=int, default=64)
    parser.add_argument('-ee', '--ee', action='store_true', help='inductive type includes ee')
    parser.add_argument('-es', '--es', action='store_true', help='inductive type includes es')
    parser.add_argument('-se', '--se', action='store_true', help='inductive type includes se')

    return parser.parse_args(args)

def save_model(model, optimizer, save_variable_list, args, steps):
    argparse_dict = vars(args)
    with open(os.path.join(args.save_path, 'config.json'), 'w') as fjson:
        json.dump(argparse_dict, fjson)

    torch.save({
        **save_variable_list,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict()},
        os.path.join(args.save_path, 'checkpoint')
    )

def set_logger(args):
    if args.do_train:
        log_file = os.path.join(args.save_path, 'train.log')
    elif args.do_valid:
        log_file = os.path.join(args.save_path, 'valid.log')
    else:
        log_file = os.path.join(args.save_path, 'test.log')

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S',
        filename=log_file,
        filemode='a+'
    )
    if args.print_on_screen:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)

def log_metrics(mode, step, metrics):
    for metric in metrics:
        logging.info('%s %s at step %d: %f' % (mode, metric, step, metrics[metric]))

def evaluate(model, easy_answers, answers, args, dataloader, query_name_dict, mode, step, writer):
    average_metrics = defaultdict(float)
    all_metrics = defaultdict(float)

    metrics = model.test_step(model, easy_answers, answers, args, dataloader)
    num_query_structures = 0
    num_queries = 0
    for query_structure in metrics:
        log_metrics(mode+" "+query_name_dict[query_structure], step, metrics[query_structure])
        for metric in metrics[query_structure]:
            writer.add_scalar("_".join([mode, query_name_dict[query_structure], metric]), metrics[query_structure][metric], step)
            all_metrics["_".join([query_name_dict[query_structure], metric])] = metrics[query_structure][metric]
            if metric != 'num_queries':
                average_metrics[metric] += metrics[query_structure][metric]
        num_queries += metrics[query_structure]['num_queries']
        num_query_structures += 1

    for metric in average_metrics:
        average_metrics[metric] /= num_query_structures
        writer.add_scalar("_".join([mode, 'average', metric]), average_metrics[metric], step)
        all_metrics["_".join(["average", metric])] = average_metrics[metric]
    log_metrics('%s average'%mode, step, average_metrics)

    return all_metrics

def load_data(args, tasks):
    logging.info("loading data")
    train_queries = pickle.load(open(os.path.join(args.data_path, "train-queries.pkl"), 'rb'))
    train_answers = pickle.load(open(os.path.join(args.data_path, "train-answers.pkl"), 'rb'))

    valid_ee_queries = pickle.load(open(os.path.join(args.data_path, "valid-ee-queries.pkl"), 'rb'))
    valid_es_queries = pickle.load(open(os.path.join(args.data_path, "valid-es-queries.pkl"), 'rb'))
    valid_se_queries = pickle.load(open(os.path.join(args.data_path, "valid-se-queries.pkl"), 'rb'))
    valid_ee_answers = pickle.load(open(os.path.join(args.data_path, "valid-ee-answers.pkl"), 'rb'))
    valid_es_answers = pickle.load(open(os.path.join(args.data_path, "valid-es-answers.pkl"), 'rb'))
    valid_se_answers = pickle.load(open(os.path.join(args.data_path, "valid-se-answers.pkl"), 'rb'))
    valid_ee_easy_answers = pickle.load(open(os.path.join(args.data_path, "valid-ee-easy-answers.pkl"), 'rb'))
    valid_es_easy_answers = pickle.load(open(os.path.join(args.data_path, "valid-es-easy-answers.pkl"), 'rb'))
    valid_se_easy_answers = pickle.load(open(os.path.join(args.data_path, "valid-se-easy-answers.pkl"), 'rb'))

    test_ee_queries = pickle.load(open(os.path.join(args.data_path, "test-ee-queries.pkl"), 'rb'))
    test_es_queries = pickle.load(open(os.path.join(args.data_path, "test-es-queries.pkl"), 'rb'))
    test_se_queries = pickle.load(open(os.path.join(args.data_path, "test-se-queries.pkl"), 'rb'))
    test_ee_answers = pickle.load(open(os.path.join(args.data_path, "test-ee-answers.pkl"), 'rb'))
    test_es_answers = pickle.load(open(os.path.join(args.data_path, "test-es-answers.pkl"), 'rb'))
    test_se_answers = pickle.load(open(os.path.join(args.data_path, "test-se-answers.pkl"), 'rb'))
    test_ee_easy_answers = pickle.load(open(os.path.join(args.data_path, "test-ee-easy-answers.pkl"), 'rb'))
    test_es_easy_answers = pickle.load(open(os.path.join(args.data_path, "test-es-easy-answers.pkl"), 'rb'))
    test_se_easy_answers = pickle.load(open(os.path.join(args.data_path, "test-se-easy-answers.pkl"), 'rb'))

    logging.info('Load pkl finished!')
    for name in all_tasks:
        if 'u' in name:
            name, evaluate_union = name.split('-')
        else:
            evaluate_union = args.evaluate_union
        if name not in tasks or evaluate_union != args.evaluate_union:
            query_structure = name_query_dict[name if 'u' not in name else '-'.join([name, evaluate_union])]
            if query_structure in train_queries:
                del train_queries[query_structure]

            if query_structure in valid_ee_queries:
                del valid_ee_queries[query_structure]
            if query_structure in valid_es_queries:
                del valid_es_queries[query_structure]
            if query_structure in valid_se_queries:
                del valid_se_queries[query_structure]

            if query_structure in test_ee_queries:
                del test_ee_queries[query_structure]
            if query_structure in test_es_queries:
                del test_es_queries[query_structure]
            if query_structure in test_se_queries:
                del test_se_queries[query_structure]

    return train_queries, train_answers, \
        valid_ee_queries, valid_es_queries, valid_se_queries, valid_ee_answers, valid_es_answers, valid_se_answers, \
        test_ee_queries, test_es_queries, test_se_queries, test_ee_answers, test_es_answers, test_se_answers, \
        valid_ee_easy_answers, valid_es_easy_answers, valid_se_easy_answers, test_ee_easy_answers, test_es_easy_answers, test_se_easy_answers

def main(args):
    set_global_seed(args.seed)

    mat = None

    if args.geo == 'ns':
        if args.use_rule:
            matpath = os.path.join(args.data_path, 'RuleAddedMat.pkl')
            if os.path.exists(matpath):
                with open(matpath, 'rb') as f:
                    mat = pickle.load(f)
            else:
                base_data = Data(args.data_path)
                mat = base_data.rel_mat
                rule_model = GraphRule(args.rule_len, args.rule_thr, base_data)
                _ = rule_model.qCalConf(rule_model.mat1, rule_model.rule_set)
                mat, _ = rule_model.updateMaxMat(rule_model.mat1, rule_model.calPath)
                exit()
        else:
            base_data = Data(args.data_path)
            mat = base_data.rel_mat


    tasks = args.tasks.split('.')
    for task in tasks:
        if 'n' in task and args.geo in ['box', 'vec']:
            assert False, "Q2B and GQE cannot handle queries with negation"

    if args.lambdas:
        lams = [float(x) for x in args.lambdas.split(';')]
        assert(len(lams) == len(tasks))
        args.lams = {name_query_dict[task if 'u' not in task else f'{task}-{args.evaluate_union}']:lams[i] for i,task in enumerate(tasks)}

    if args.evaluate_union == 'DM':
        assert args.geo == 'beta', "only BetaE supports modeling union using De Morgan's Laws"

    cur_time = parse_time()
    if args.prefix is None:
        prefix = 'logs'
    else:
        prefix = args.prefix

    print ("overwritting args.save_path")
    args.save_path = os.path.join(prefix, args.data_path.split('/')[-1], args.tasks, args.geo)
    if args.geo in ['box']:
        tmp_str = "g-{}-mode-{}".format(args.gamma, args.box_mode)
    elif args.geo in ['vec']:
        tmp_str = "g-{}".format(args.gamma)
    elif args.geo == 'beta':
        tmp_str = "g-{}-mode-{}".format(args.gamma, args.beta_mode)
    elif args.geo == 'ns':
        tmp_str = "g-{}-mode-{}".format(args.gamma, args.kge_mode)

    if args.checkpoint_path is not None:
        args.save_path = args.checkpoint_path
    else:
        args.save_path = os.path.join(args.save_path, tmp_str, cur_time)

    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)

    print ("logging to", args.save_path)
    if not args.do_train:
        writer = SummaryWriter('./logs-debug/unused-tb')
    else:
        writer = SummaryWriter(args.save_path)
    set_logger(args)

    with open('%s/stats.txt'%args.data_path) as f:
        entrel = f.readlines()
        nentity = int(entrel[0].split(' ')[-1])
        nrelation = int(entrel[1].split(' ')[-1])

    args.nentity = nentity
    args.nrelation = nrelation

    logging.info('-------------------------------'*3)
    logging.info('Geo: %s' % args.geo)
    logging.info('Data Path: %s' % args.data_path)
    logging.info('#entity: %d' % nentity)
    logging.info('#relation: %d' % nrelation)
    logging.info('#max steps: %d' % args.max_steps)
    logging.info('Evaluate unoins using: %s' % args.evaluate_union)

    train_queries, train_answers, valid_ee_queries, valid_es_queries, valid_se_queries, valid_ee_answers, valid_es_answers, valid_se_answers, \
        test_ee_queries, test_es_queries, test_se_queries, test_ee_answers, test_es_answers, test_se_answers,\
            valid_ee_easy_answers, valid_es_easy_answers, valid_se_easy_answers, test_ee_easy_answers, test_es_easy_answers, test_se_easy_answers = load_data(args, tasks)

    logging.info("Training info:")
    if args.do_train:
        for query_structure in train_queries:
            logging.info(query_name_dict[query_structure]+": "+str(len(train_queries[query_structure])))
        train_path_queries = defaultdict(set)
        train_other_queries = defaultdict(set)
        path_list = ['1p', '2p', '3p']
        for query_structure in train_queries:
            if query_name_dict[query_structure] in path_list:
                train_path_queries[query_structure] = train_queries[query_structure]
            else:
                train_other_queries[query_structure] = train_queries[query_structure]
        train_path_queries = flatten_query(train_path_queries)
        train_path_iterator = SingledirectionalOneShotIterator(DataLoader(
                                    TrainDataset(train_path_queries, nentity, nrelation, args.negative_sample_size, train_answers),
                                    batch_size=args.batch_size,
                                    shuffle=True,
                                    num_workers=args.cpu_num,
                                    collate_fn=TrainDataset.collate_fn
                                ))
        if len(train_other_queries) > 0:
            train_other_queries = flatten_query(train_other_queries)
            train_other_iterator = SingledirectionalOneShotIterator(DataLoader(
                                        TrainDataset(train_other_queries, nentity, nrelation, args.negative_sample_size, train_answers),
                                        batch_size=args.batch_size,
                                        shuffle=True,
                                        num_workers=args.cpu_num,
                                        collate_fn=TrainDataset.collate_fn
                                    ))
        else:
            train_other_iterator = None

    logging.info("Validation info:")
    if args.do_valid:
        if args.ee:
            for query_structure in valid_ee_queries:
                logging.info('ee_' +  query_name_dict[query_structure]+": "+str(len(valid_ee_queries[query_structure])))
            valid_ee_queries = flatten_query(valid_ee_queries)
            valid_ee_dataloader = DataLoader(
                TestDataset(
                    valid_ee_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )
        if args.es:
            for query_structure in valid_es_queries:
                logging.info('es_' + query_name_dict[query_structure]+": "+str(len(valid_es_queries[query_structure])))
            valid_es_queries = flatten_query(valid_es_queries)
            valid_es_dataloader = DataLoader(
                TestDataset(
                    valid_es_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )
        if args.se:
            for query_structure in valid_se_queries:
                logging.info('se_' + query_name_dict[query_structure]+": "+str(len(valid_se_queries[query_structure])))
            valid_se_queries = flatten_query(valid_se_queries)
            valid_se_dataloader = DataLoader(
                TestDataset(
                    valid_se_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )

    logging.info("Test info:")
    if args.do_test:
        if args.ee:
            for query_structure in test_ee_queries:
                logging.info('ee_' + query_name_dict[query_structure]+": "+str(len(test_ee_queries[query_structure])))
            test_ee_queries = flatten_query(test_ee_queries)
            test_ee_dataloader = DataLoader(
                TestDataset(
                    test_ee_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )
        if args.es:
            for query_structure in test_es_queries:
                logging.info('es_' + query_name_dict[query_structure]+": "+str(len(test_es_queries[query_structure])))
            test_es_queries = flatten_query(test_es_queries)
            test_es_dataloader = DataLoader(
                TestDataset(
                    test_es_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )
        if args.se:
            for query_structure in test_se_queries:
                logging.info('se_' + query_name_dict[query_structure]+": "+str(len(test_se_queries[query_structure])))
            test_se_queries = flatten_query(test_se_queries)
            test_se_dataloader = DataLoader(
                TestDataset(
                    test_se_queries,
                    args.nentity,
                    args.nrelation,
                ),
                batch_size=args.test_batch_size,
                num_workers=args.cpu_num,
                collate_fn=TestDataset.collate_fn
            )

    print('Building neighbor graph...')
    inductiveGraph = neighborGraph(args)
    print('Neighbor graph finished!')


    model = KGReasoning(
        nentity=nentity,
        nrelation=nrelation,
        hidden_dim=args.hidden_dim,
        gamma=args.gamma,
        geo=args.geo,
        mode=args.kge_mode,
        use_cuda = args.cuda,
        box_mode=eval_tuple(args.box_mode),
        beta_mode = eval_tuple(args.beta_mode),
        test_batch_size=args.test_batch_size,
        query_name_dict = query_name_dict,
        mat = mat,
        inductiveGraph = inductiveGraph,
        loss_weight = args.loss_weight,
        args = args
    )

    logging.info('Model Parameter Configuration:')
    num_params = 0
    for name, param in model.named_parameters():
        logging.info('Parameter %s: %s, require_grad = %s' % (name, str(param.size()), str(param.requires_grad)))
        if param.requires_grad:
            num_params += np.prod(param.size())
    logging.info('Parameter Number: %d' % num_params)

    if args.cuda:
        model = model.cuda()


    if args.KGE_pretrain:
        pre = torch.load(os.path.join(args.data_path, 'KGEmodel', args.kge_mode+'.ckpt'))
        pretrained_dict = { 'embedding_range':pre['state_dict']['model.embedding_range'], \
            'entity_embedding':pre['state_dict']['model.ent_emb.weight'], 'relation_embedding':pre['state_dict']['model.rel_emb.weight']}
        model_dict = model.state_dict()
        model_dict.update(pretrained_dict)
        model.load_state_dict(model_dict)

    if args.do_train:
        current_learning_rate = args.learning_rate
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, model.parameters()),
            lr=current_learning_rate
        )
        warm_up_steps = args.max_steps // 2


    if args.checkpoint_path is not None:
        logging.info('Loading checkpoint %s...' % args.checkpoint_path)
        checkpoint = torch.load(os.path.join(args.checkpoint_path, 'checkpoint'))
        init_step = checkpoint['step']
        model.load_state_dict(checkpoint['model_state_dict'])

        if args.do_train:
            current_learning_rate = checkpoint['current_learning_rate']
            warm_up_steps = checkpoint['warm_up_steps']
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    else:
        logging.info('Ramdomly Initializing %s Model...' % args.geo)
        init_step = 0

    step = init_step
    if args.geo == 'box':
        logging.info('box mode = %s' % args.box_mode)
    elif args.geo == 'beta':
        logging.info('beta mode = %s' % args.beta_mode)
    elif args.geo == 'ns':
        logging.info('kge mode = %s' % args.kge_mode)

    logging.info('tasks = %s' % args.tasks)
    logging.info('init_step = %d' % init_step)
    if args.do_train:
        logging.info('Start Training...')
        logging.info('learning_rate = %.10f' % current_learning_rate)
    logging.info('batch_size = %d' % args.batch_size)
    logging.info('hidden_dim = %d' % args.hidden_dim)
    logging.info('gamma = %f' % args.gamma)

    if args.do_train:
        training_logs = []
        for step in range(init_step, args.max_steps):
            if step == 2*args.max_steps//3:
                args.valid_steps *= 4

            with torch.autograd.set_detect_anomaly(True):
                log = model.train_step(model, optimizer, train_path_iterator, args, step)
            for metric in log:
                writer.add_scalar('path_'+metric, log[metric], step)
            if train_other_iterator is not None:
                log = model.train_step(model, optimizer, train_other_iterator, args, step)
                for metric in log:
                    writer.add_scalar('other_'+metric, log[metric], step)
                log = model.train_step(model, optimizer, train_path_iterator, args, step)

            training_logs.append(log)

            if step >= warm_up_steps:
                current_learning_rate = current_learning_rate / 5
                logging.info('Change learning_rate to %.10f at step %d' % (current_learning_rate, step))
                optimizer = torch.optim.Adam(
                    filter(lambda p: p.requires_grad, model.parameters()),
                    lr=current_learning_rate
                )
                warm_up_steps = args.max_steps

            if step % args.save_checkpoint_steps == 0:
                save_variable_list = {
                    'step': step,
                    'current_learning_rate': current_learning_rate,
                    'warm_up_steps': warm_up_steps
                }
                save_model(model, optimizer, save_variable_list, args, step)

            if step % args.valid_steps == 0 and step > init_step:
                if args.do_valid:
                    logging.info('Evaluating on Valid Dataset...')
                    if args.ee:
                        valid_ee_metrics = evaluate(model, valid_ee_easy_answers, valid_ee_answers, args, valid_ee_dataloader, query_name_dict, 'Valid_ee', step, writer)
                    if args.es:
                        valid_es_metrics = evaluate(model, valid_es_easy_answers, valid_es_answers, args, valid_es_dataloader, query_name_dict, 'Valid_es', step, writer)
                    if args.se:
                        valid_se_metrics = evaluate(model, valid_se_easy_answers, valid_se_answers, args, valid_se_dataloader, query_name_dict, 'Valid_se', step, writer)

                if args.do_test:
                    logging.info('Evaluating on Test Dataset...')
                    if args.ee:
                        test_ee_metrics = evaluate(model, test_ee_easy_answers, test_ee_answers, args, test_ee_dataloader, query_name_dict, 'Test_ee', step, writer)
                    if args.es:
                        test_es_metrics = evaluate(model, test_es_easy_answers, test_es_answers, args, test_es_dataloader, query_name_dict, 'Test_es', step, writer)
                    if args.se:
                        test_se_metrics = evaluate(model, test_se_easy_answers, test_se_answers, args, test_se_dataloader, query_name_dict, 'Test_se', step, writer)

            if step % args.log_steps == 0:
                metrics = {}
                for metric in training_logs[0].keys():
                    metrics[metric] = sum([log[metric] for log in training_logs])/len(training_logs)

                log_metrics('Training average', step, metrics)
                training_logs = []

        save_variable_list = {
            'step': step,
            'current_learning_rate': current_learning_rate,
            'warm_up_steps': warm_up_steps
        }
        save_model(model, optimizer, save_variable_list, args, step)

    try:
        print (step)
    except:
        step = 0

    if args.do_test:
        logging.info('Evaluating on Test Dataset...')
        if args.ee:
            test_ee_metrics = evaluate(model, test_ee_easy_answers, test_ee_answers, args, test_ee_dataloader, query_name_dict, 'Test_ee', step, writer)
        if args.es:
            test_es_metrics = evaluate(model, test_es_easy_answers, test_es_answers, args, test_es_dataloader, query_name_dict, 'Test_es', step, writer)
        if args.se:
            test_se_metrics = evaluate(model, test_se_easy_answers, test_se_answers, args, test_se_dataloader, query_name_dict, 'Test_se', step, writer)

    logging.info("Training finished!!")

if __name__ == '__main__':
    main(parse_args())