import argparse
import logging

from distutils.util import strtobool

import tensorflow as tf

from generic.data_provider.iterator import Iterator
from generic.tf_utils.evaluator import Evaluator
from generic.tf_utils.optimizer import create_optimizer
from generic.utils.config import load_config
from generic.data_provider.image_loader import get_img_builder, _create_image_builder_rcnn
from generic.data_provider.nlp_utils import GloveEmbeddings
from generic.utils.thread_pool import create_cpu_pool

from guesswhat.data_provider.guesswhat_dataset import Dataset
from guesswhat.data_provider.guesswhat_dataset import Dataset_visg
from guesswhat.data_provider.guesswhat_tokenizer_orig import GWTokenizer
from guesswhat.models.guesser.guesser_factory import create_guesser


if __name__ == '__main__':

    ###############################
    #  LOAD CONFIG
    #############################

    parser = argparse.ArgumentParser('Guesser network baseline!')

    parser.add_argument("-data_dir", type=str, default="data", help="Directory with data")
    parser.add_argument("-out_dir", type=str, default="out/guesser", help="Directory in which experiments are stored")
    parser.add_argument("-config", type=str, default="config/guesser/config.baseline.json", help='Config file')
    parser.add_argument("-dict_file", type=str, default="data/dict.json", help="Dictionary file name")
    parser.add_argument("-glove_file", type=str, default="glove_dict.pkl", help="Glove file name")
    parser.add_argument("-img_dir", type=str, help='Directory with images')
    parser.add_argument("-crop_dir", type=str, help='Directory with crops')
    parser.add_argument("-load_checkpoint", type=str, help="Load model parameters from specified checkpoint")
    parser.add_argument("-continue_exp", type=lambda x: bool(strtobool(x)), default="False", help="Continue previously started experiment?")
    parser.add_argument("-gpu_ratio", type=float, default=0.5, help="How many GPU ram is required? (ratio)")
    parser.add_argument("-early_stop", type=int, default=5)
    parser.add_argument("-skip_training",  type=lambda x: bool(strtobool(x)), default="False", help="Start from checkpoint?")
    parser.add_argument("-no_thread", type=int, default=4, help="No thread to load batch")
    parser.add_argument("-train_epoch", type=int, default=30, help="No thread to load batch")
    parser.add_argument("-no_games_to_load", type=int, default=float("inf"), help="No games to use during training Default : all")
    parser.add_argument("-load_new",  type=lambda x: bool(strtobool(x)), default="True", help="Start from checkpoint?")

    args = parser.parse_args()

    config, xp_manager = load_config(args)
    logger = logging.getLogger()

    # Load config
    batch_size = config['optimizer']['batch_size']
    no_epoch = args.train_epoch

    if args.load_new and config['model']['image']['image_input'] == "rcnn":
        rcnn = True
        print("rcnn!")
    else:
        rcnn = False

    ###############################
    #  LOAD DATA
    #############################

    # Load image
    # Load image
    image_builder, crop_builder = None, None
    use_resnet, use_process = False, False
    if rcnn:
        image_builder = _create_image_builder_rcnn()
    elif config["model"]['inputs'].get('image', False):
        logger.info('Loading images..')
        image_builder = get_img_builder(config['model']['image'], args.img_dir)
        use_resnet = image_builder.is_raw_image()

    if config["model"]['inputs'].get('crop', False):
        logger.info('Loading crops..')
        crop_builder = get_img_builder(config['model']['crop'], args.crop_dir, is_crop=True)
        use_resnet = crop_builder.is_raw_image()
        use_resnet |= image_builder.is_raw_image()
        use_process |= image_builder.require_multiprocess()

    # Load data
    logger.info('Loading data..')
    trainset = Dataset(args.data_dir, "train", image_builder, crop_builder, rcnn, args.no_games_to_load)
    validset = Dataset(args.data_dir, "valid", image_builder, crop_builder, rcnn, args.no_games_to_load)
    testset = Dataset_visg("/home/xzp/guesswhat_v2/data/nag2.json", image_builder, crop_builder, rcnn, args.no_games_to_load)

    # Load dictionary
    logger.info('Loading dictionary..')
    tokenizer = GWTokenizer(args.dict_file)

    # Load glove
    glove = None
    # if config["model"]["question"]['glove']:
    #     logger.info('Loading glove..')
    #     glove = GloveEmbeddings(args.glove_file)

    # Build Network
    logger.info('Building network..')
    network, batchifier_cstor, listener = create_guesser(config["model"], num_words=tokenizer.no_words)

    # Build Optimizer
    logger.info('Building optimizer..')
    optimizer, outputs = create_optimizer(network, config["optimizer"])

    ###############################
    #  START  TRAINING
    #############################

    # create a saver to store/load checkpoint
    saver = tf.train.Saver()

    # CPU/GPU option
    config_gpu = tf.ConfigProto()
    config_gpu.gpu_options.allow_growth = True
    # gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=args.gpu_ratio)
    # config_gpu = tf.ConfigProto(gpu_options=gpu_options)

    with tf.Session(config=config_gpu) as sess:
        sources = network.get_sources(sess)
        logger.info("Sources: " + ', '.join(sources))

        sess.run(tf.global_variables_initializer())
        if args.continue_exp or args.load_checkpoint is not None:
            start_epoch = xp_manager.load_checkpoint(sess, saver)
        else:
            start_epoch = 0

        # create training tools
        evaluator = Evaluator(sources, network.scope_name, network=network, tokenizer=tokenizer)
        batchifier = batchifier_cstor(tokenizer, sources, glove=glove, status=('success',))
        xp_manager.configure_score_tracking("valid_accuracy", max_is_best=True)

        for t in range(start_epoch, no_epoch):
            if args.skip_training:
                logger.info("Skip training...")
                break
            logger.info('Epoch {}..'.format(t + 1))

            # Create cpu pools (at each iteration otherwise threads may become zombie - python bug)
            cpu_pool = create_cpu_pool(args.no_thread, use_process=use_process)

            train_iterator = Iterator(trainset,
                                      batch_size=batch_size, pool=cpu_pool,
                                      batchifier=batchifier,
                                      shuffle=True)
            train_loss, _ = evaluator.process(sess, train_iterator, outputs=outputs + [optimizer], listener=listener)
            train_accuracy = listener.accuracy()  # Some guessers needs to go over the full dataset before comuting the accuracy, thus we use an intermediate listener

            valid_iterator = Iterator(validset, pool=cpu_pool,
                                      batch_size=batch_size*2,
                                      batchifier=batchifier,
                                      shuffle=False)
            valid_loss, _ = evaluator.process(sess, valid_iterator, outputs=outputs, listener=listener)
            valid_accuracy = listener.accuracy()

            logger.info("Training loss      : {}".format(train_loss))
            logger.info("Training accuracy  : {}".format(train_accuracy))
            logger.info("Validation loss    : {}".format(valid_loss))
            logger.info("Validation accuracy: {}".format(valid_accuracy))

            stop_flag = xp_manager.save_checkpoint(sess, saver,
                                       epoch=t,
                                       losses=dict(
                                           train_loss=train_loss,
                                           valid_loss=valid_loss,
                                           train_accuracy=train_accuracy,
                                           valid_accuracy=valid_accuracy,
                                       ))

            if stop_flag >= args.early_stop:
                logger.info("==================early stopping===================")
                break

        # Load early stopping
        xp_manager.load_checkpoint(sess, saver, load_best=True)
        cpu_pool = create_cpu_pool(args.no_thread, use_process=use_process)

        # Create Listener
        test_iterator = Iterator(testset, pool=cpu_pool,
                                 batch_size=batch_size*2,
                                 batchifier=batchifier,
                                 shuffle=False)
        [test_loss, _] = evaluator.process(sess, test_iterator, outputs=outputs, listener=listener)
        test_accuracy = listener.accuracy()

        logger.info("Testing loss: {}".format(test_loss))
        logger.info("Testing error: {}".format(1-test_accuracy))
        logger.info("Testing accuracy: {}".format(test_accuracy))

        # Save the test scores
        xp_manager.update_user_data(
            user_data={
                "test_loss": test_loss,
                "test_accuracy": test_accuracy,
            }
        )

