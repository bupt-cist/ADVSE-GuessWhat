import argparse
import os

import logging
from distutils.util import strtobool

import tensorflow as tf

from generic.utils.thread_pool import create_cpu_pool
from generic.utils.config import load_config, get_config_from_xp

from generic.tf_utils.optimizer import create_optimizer

from generic.data_provider.image_loader import get_img_builder, _create_image_builder_rcnn
from generic.data_provider.iterator import Iterator
from generic.tf_utils.evaluator import Evaluator

from guesswhat.models.qgen.rl_module import PolicyGradient
from guesswhat.models.qgen.qgen_factory import create_qgen
from guesswhat.models.guesser.guesser_factory import create_guesser
from guesswhat.models.oracle.oracle_factory import create_oracle

from guesswhat.models.looper.basic_looper import BasicLooper

from guesswhat.models.qgen.qgen_wrapper import QGenWrapper
from guesswhat.models.oracle.oracle_wrapper import OracleWrapper
from guesswhat.models.guesser.guesser_wrapper import GuesserWrapper

# from guesswhat.data_provider.oracle_batchifier import BatchifierSplitMode
from guesswhat.data_provider.guesswhat_dataset import Dataset
from guesswhat.data_provider.looper_batchifier import LooperBatchifier
from guesswhat.data_provider.guesswhat_tokenizer import GWTokenizer

from guesswhat.train.utils import test_models, compute_qgen_accuracy


if __name__ == '__main__':

    parser = argparse.ArgumentParser('Question generator (policy gradient baseline))')

    parser.add_argument("-data_dir", type=str, default="data", help="Directory with data")
    parser.add_argument("-out_dir", default="out/looper", type=str, help="Directory in which experiments are stored")
    parser.add_argument("-img_dir", type=str, default='data/features/vgg16/image.hdf5', help='Directory with images')
    parser.add_argument("-crop_dir", type=str, help='Directory with images')
    parser.add_argument("-config", type=str, default="config/looper/config.rnn.json", help='Config file')
    parser.add_argument("-dict_file", type=str, default="dict.json", help="Dictionary file name")

    parser.add_argument("-networks_dir", type=str, default='out', help="Directory with pretrained networks")

    parser.add_argument("-continue_exp", type=lambda x: bool(strtobool(x)), default="False", help="Continue previously started experiment?")
    parser.add_argument("-load_checkpoint", type=str, help="Start from checkpoint?")
    parser.add_argument("-train_epoch", type=int, default=500)

    parser.add_argument("-skip_training",  type=lambda x: bool(strtobool(x)), default="False", help="Start from checkpoint?")
    parser.add_argument("-evaluate_all", type=lambda x: bool(strtobool(x)), default="False", help="Evaluate sampling, greedy and BeamSearch?")  #TODO use an input list
    # parser.add_argument("-store_games", type=lambda x: bool(strtobool(x)), default="True", help="Should we dump the game at evaluation times")
    parser.add_argument("-no_games_to_load", type=int, default=float("inf"), help="No games to use during training Default : all")

    parser.add_argument("-gpu_ratio", type=float, default=0.95, help="How muany GPU ram is required? (ratio)")
    parser.add_argument("-no_thread", type=int, default=4, help="No thread to load batch")
    parser.add_argument("-load_new",  type=lambda x: bool(strtobool(x)), default="True", help="Start from checkpoint?")
    parser.add_argument("-test_ini",  type=lambda x: bool(strtobool(x)), default="True", help="Start from checkpoint?")
    parser.add_argument("-use_redis",  type=lambda x: bool(strtobool(x)), default="False", help="Start from checkpoint?")

    args = parser.parse_args()

    no_epoch = args.train_epoch

    loop_config, xp_manager = load_config(args)
    logger = logging.getLogger()

    # Load all  networks configs
    oracle_config = get_config_from_xp(os.path.join(args.networks_dir, "oracle"), loop_config["oracle_identifier"])
    guesser_config = get_config_from_xp(os.path.join(args.networks_dir, "guesser"), loop_config["guesser_identifier"])
    qgen_config = get_config_from_xp(os.path.join(args.networks_dir, "qgen"), loop_config["qgen_identifier"])

    ###############################
    #  LOAD DATA
    #############################

    # Load image

    if args.load_new and qgen_config['model']['image']['image_input'] == "rcnn":
        rcnn = True
        print("rcnn!")
    else:
        rcnn = False

    logger.info('Loading images..')

    if rcnn:
        image_builder = _create_image_builder_rcnn(args.use_redis)
    elif qgen_config["model"]['inputs'].get('image', False):
        logger.info('Loading images..')
        image_builder = get_img_builder(qgen_config['model']['image'], args.img_dir)

    crop_builder = None
    if oracle_config['model']['inputs'].get('crop', False):
        logger.info('Loading crops..')
        crop_builder = get_img_builder(oracle_config['model']['crop'], args.crop_dir, is_crop=True)

    # Load data
    logger.info('Loading data..')
    trainset = Dataset(args.data_dir, "train", image_builder, crop_builder, rcnn, args.no_games_to_load)
    validset = Dataset(args.data_dir, "valid", image_builder, crop_builder, rcnn, args.no_games_to_load)
    testset = Dataset(args.data_dir, "test", image_builder, crop_builder, rcnn, args.no_games_to_load)

    # Load dictionary
    logger.info('Loading dictionary..')
    tokenizer = GWTokenizer(os.path.join(args.data_dir, args.dict_file))

    ###############################
    #  LOAD NETWORKS
    #############################

    rl_module = PolicyGradient(stop_gradient=True)

    logger.info('Building networks..')

    qgen_network, qgen_batchifier_cstor = create_qgen(qgen_config["model"], num_words=tokenizer.no_words, rl_module=rl_module)
    qgen_var = [v for v in tf.global_variables() if "qgen" in v.name]  # and 'rl_baseline' not in v.name
    qgen_saver = tf.train.Saver(var_list=qgen_var)

    oracle_network, oracle_batchifier_cstor = create_oracle(oracle_config["model"], num_words=tokenizer.no_words)
    oracle_var = [v for v in tf.global_variables() if "oracle" in v.name]
    oracle_saver = tf.train.Saver(var_list=oracle_var)

    guesser_network, guesser_batchifier_cstor, guesser_listener = create_guesser(guesser_config["model"], num_words=tokenizer.no_words)
    guesser_var = [v for v in tf.global_variables() if "guesser" in v.name]
    guesser_saver = tf.train.Saver(var_list=guesser_var)

    loop_saver = tf.train.Saver(allow_empty=False)

    ###############################
    #  REINFORCE OPTIMIZER
    #############################

    logger.info('Building optimizer..')
    optimizer, _ = create_optimizer(qgen_network, loop_config["optimizer"],
                                    optim_cst=eval("tf.train." + loop_config["optimizer"]["name"]),
                                    accumulate_gradient=qgen_network.is_seq2seq())

    ###############################
    #  START TRAINING
    #############################

    # Load config
    batch_size = loop_config['optimizer']['batch_size']
    no_epoch = loop_config["optimizer"]["no_epoch"]

    # mode_to_evaluate = ["sampling", "beam"]
    mode_to_evaluate = ["greedy"]
    if args.evaluate_all:
        mode_to_evaluate = ["greedy", "sampling", "beam"]

    # CPU/GPU option
    gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=args.gpu_ratio)
    config = tf.ConfigProto(gpu_options=gpu_options)

    with tf.Session(config=config) as sess:

        ###############################
        #  LOAD PRE-TRAINED NETWORK
        #############################

        sess.run(tf.global_variables_initializer())
        if args.continue_exp or args.load_checkpoint is not None:
            start_epoch = xp_manager.load_checkpoint(sess, qgen_saver)
        else:
            qgen_var_supervized = [v for v in tf.global_variables() if "qgen" in v.name and 'value_function' not in v.name]
            qgen_loader_supervized = tf.train.Saver(var_list=qgen_var_supervized)
            qgen_loader_supervized.restore(sess, os.path.join(args.networks_dir, 'qgen', loop_config["qgen_identifier"], 'best', 'params.ckpt'))
            start_epoch = 0

        oracle_saver.restore(sess, os.path.join(args.networks_dir, 'oracle', loop_config["oracle_identifier"], 'best', 'params.ckpt'))
        guesser_saver.restore(sess, os.path.join(args.networks_dir, 'guesser', loop_config["guesser_identifier"], 'best', 'params.ckpt'))

        # create training tools
        loop_sources = qgen_network.get_sources(sess)
        logger.info("Sources: " + ', '.join(loop_sources))

        train_batchifier = LooperBatchifier(tokenizer, generate_new_games=True)
        eval_batchifier = LooperBatchifier(tokenizer, generate_new_games=False)

        # Initialize the looper to eval/train the game-simulation

        qgen_batchifier = qgen_batchifier_cstor(tokenizer, sources=qgen_network.get_sources(sess), generate=True)
        qgen_wrapper = QGenWrapper(qgen_network, qgen_batchifier, tokenizer,
                                   max_length=loop_config['loop']['max_depth'],
                                   k_best=loop_config['loop']['beam_k_best'])

        oracle_split_mode = 1
        # oracle_split_mode = BatchifierSplitMode.from_string(oracle_config["model"]["question"]["input_type"])
        oracle_batchifier = oracle_batchifier_cstor(tokenizer, sources=oracle_network.get_sources(sess), split_mode=oracle_split_mode)
        oracle_wrapper = OracleWrapper(oracle_network, oracle_batchifier, tokenizer)

        guesser_batchifier = guesser_batchifier_cstor(tokenizer, sources=guesser_network.get_sources(sess))
        guesser_wrapper = GuesserWrapper(guesser_network, guesser_batchifier, tokenizer, guesser_listener)

        xp_manager.configure_score_tracking("valid_accuracy", max_is_best=True)
        game_engine = BasicLooper(loop_config,
                                  oracle_wrapper=oracle_wrapper,
                                  guesser_wrapper=guesser_wrapper,
                                  qgen_wrapper=qgen_wrapper,
                                  tokenizer=tokenizer,
                                  batch_size=loop_config["optimizer"]["batch_size"])

        # Compute the initial scores
        if args.test_ini:
            logger.info(">>>-------------- INITIAL SCORE ---------------------<<<")
            # evaluator = Evaluator(loop_sources, qgen_network.scope_name, network=qgen_network, tokenizer=tokenizer)
            cpu_pool = create_cpu_pool(args.no_thread, use_process=False)

            logger.info(">>>  Initial models  <<<")
            test_models(sess, testset, cpu_pool=cpu_pool, batch_size=batch_size*2,
                        oracle=oracle_network, oracle_batchifier=oracle_batchifier,
                        guesser=guesser_network, guesser_batchifier=guesser_batchifier, guesser_listener=guesser_listener,
                        qgen=qgen_network, qgen_batchifier=qgen_batchifier)

            logger.info(">>>  New Objects  <<<")
            compute_qgen_accuracy(sess, trainset, batchifier=train_batchifier, looper=game_engine,
                                  mode=mode_to_evaluate, cpu_pool=cpu_pool, batch_size=batch_size,
                                  name="ini.new_object", save_path=xp_manager.dir_xp, store_games=True)

            logger.info(">>>  New Games  <<<")
            compute_qgen_accuracy(sess, testset, batchifier=eval_batchifier, looper=game_engine,
                                  mode=mode_to_evaluate, cpu_pool=cpu_pool, batch_size=batch_size,
                                  name="ini.new_images", save_path=xp_manager.dir_xp, store_games=True)
            logger.info(">>>------------------------------------------------<<<")


        logs = []
        # Start training
        final_val_score = 0.
        for epoch in range(no_epoch):
            if args.skip_training:
                logger.info("Skip training...")
                break
            logger.info("Epoch {}/{}".format(epoch, no_epoch))

            cpu_pool = create_cpu_pool(args.no_thread, use_process=False)

            train_iterator = Iterator(trainset,
                                      batch_size=batch_size,
                                      pool=cpu_pool,
                                      shuffle=True,
                                      batchifier=train_batchifier)

            [train_accuracy, _] = game_engine.process(sess, train_iterator,
                                                      optimizer=optimizer,
                                                      mode="sampling")

            valid_iterator = Iterator(validset, pool=cpu_pool,
                                      batch_size=batch_size,
                                      batchifier=eval_batchifier,
                                      shuffle=False)
            [val_accuracy, games] = game_engine.process(sess, valid_iterator, mode="sampling")

            logger.info("Accuracy (train - sampling) : {}".format(train_accuracy))
            logger.info("Accuracy (valid - sampling) : {}".format(val_accuracy))
            # val_accuracy = train_accuracy

            xp_manager.save_checkpoint(sess, qgen_saver,
                                       epoch=epoch,
                                       losses=dict(
                                           train_accuracy=train_accuracy,
                                           valid_accuracy=val_accuracy,
                                       ))

        logger.info(">>>-------------- FINAL SCORE ---------------------<<<")

        # Load early stopping
        xp_manager.load_checkpoint(sess, qgen_saver, load_best=True)
        cpu_pool = create_cpu_pool(args.no_thread, use_process=image_builder.require_multiprocess())

        logger.info(">>>  New Objects  <<<")
        compute_qgen_accuracy(sess, trainset, batchifier=train_batchifier, looper=game_engine,
                              mode=mode_to_evaluate, cpu_pool=cpu_pool, batch_size=batch_size,
                              name="new_object", save_path=xp_manager.dir_xp, store_games=True)

        logger.info(">>>  New Games  <<<")
        compute_qgen_accuracy(sess, testset, batchifier=eval_batchifier, looper=game_engine,
                              mode=mode_to_evaluate, cpu_pool=cpu_pool, batch_size=batch_size,
                              name="new_games", save_path=xp_manager.dir_xp, store_games=True)
        logger.info(">>>------------------------------------------------<<<")
