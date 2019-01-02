import tensorflow as tf
import data_pipeline as dp
import math
import sys
sys.path.insert(0, '../utilities')
import utilities

FLAGS = tf.flags.FLAGS
tf.flags.DEFINE_integer('batch_size', 128, 'Batch size. ')
tf.flags.DEFINE_integer('total_epochs', 100, 'Number of epochs for training')
tf.flags.DEFINE_float('learning_rate', 0.001, 'Constant learning rate')
tf.flags.DEFINE_string('mode', 'train', 'Run the model in "train", "eval" or "predict" mode')
tf.flags.DEFINE_string('step', 'target', 'Either source or target - switch between source only training and '
                                         'target adversarial training')
tf.flags.DEFINE_integer('channel_size', 3, 'Either 1 or 3. Defaults to 3.')

tf.logging.set_verbosity(tf.logging.DEBUG)


def estimator_model_fn(features, labels, mode, params):
    """The estimator function"""
    batch_size = tf.shape(features['source'])[0]

    if mode == tf.estimator.ModeKeys.PREDICT:
        # TODO: To be implemented
        return

    if FLAGS.step == 'source':
        input_layer_source = tf.feature_column.input_layer({"source": features['source']}, params['feature_columns'])
        # CNNs need input data to be of shape [batch_size, width, height, channel]
        input_layer_source = tf.reshape(input_layer_source, [batch_size, 28, 28, params['channel_size']])

        out = lenet_encoder(input_layer_source, scope='source_encoder')
        with tf.variable_scope('classifier', reuse=tf.AUTO_REUSE):
            logits = tf.layers.dense(out, 10)

        # Gotta change labels to one-hot
        class_labels = tf.one_hot(labels['labels'], 10)

        # Compute loss
        class_loss = tf.losses.softmax_cross_entropy(class_labels, logits=logits)

        # Get predicted classes
        predicted_classes_source = tf.argmax(logits, axis=1, output_type=tf.int32)

        # Evaluate if in EVAL
        if mode == tf.estimator.ModeKeys.EVAL:
            source_class_acc = tf.metrics.accuracy(labels=labels['labels'],
                                                   predictions=predicted_classes_source,
                                                   name='source_class_acc_op')
            metrics = {'source_class_acc': source_class_acc}
            return tf.estimator.EstimatorSpec(
                mode, loss=class_loss, eval_metric_ops=metrics)

        # Calculate a non streaming (per batch) accuracy
        source_class_acc = utilities.non_streaming_accuracy(predicted_classes_source,
                                                            tf.cast(labels['labels'], tf.int32))

        # Initialize learning rate
        # tf.summary.scalar('class_loss', class_loss)
        # tf.summary.scalar('source_class_acc', source_class_acc)
        tf.identity(class_loss, 'loss')
        tf.identity(source_class_acc, 'source_class_acc')
        optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(class_loss, global_step=tf.train.get_global_step())
        return tf.estimator.EstimatorSpec(mode, loss=class_loss, train_op=train_op)
    if FLAGS.step == 'target':
        # Now train adversarial
        input_layer_source = tf.feature_column.input_layer({"source": features['source']}, params['feature_columns'][0])
        input_layer_target = tf.feature_column.input_layer({"target": features['target']}, params['feature_columns'][1])
        # CNNs need input data to be of shape [batch_size, width, height, channel]
        input_layer_source = tf.reshape(input_layer_source, [batch_size, 28, 28, params['channel_size']])
        input_layer_target = tf.reshape(input_layer_target, [batch_size, 32, 32, params['channel_size']])
        # Resize SVHN source to 28 x 28
        input_layer_target = tf.image.resize_images(input_layer_target, [28, 28])
        # Get source and target encoded vectors.
        out_source = lenet_encoder(input_layer_source, scope='source_encoder', trainable=False)
        out_target = lenet_encoder(input_layer_target, scope='target_encoder', trainable=True)
        out_target_ = lenet_encoder(input_layer_target, scope='source_encoder', trainable=False)
        # Classify target for non-streaming accuracy
        with tf.variable_scope('classifier', reuse=tf.AUTO_REUSE):
            class_logits_target = tf.layers.dense(out_target, 10, trainable=False)
        with tf.variable_scope('classifier', reuse=tf.AUTO_REUSE):
            class_logits_source = tf.layers.dense(out_source, 10, trainable=False)
        with tf.variable_scope('classifier', reuse=tf.AUTO_REUSE):
            class_logits_target_ = tf.layers.dense(out_target_, 10, trainable=False)
        # Initialize source encoder with pretrained weights
        tf.train.init_from_checkpoint(tf.train.latest_checkpoint('./model_m2mm/source_model/'),
                                      {'source_encoder/': 'source_encoder/', 'classifier/': 'classifier/'})
        if tf.train.latest_checkpoint('./model_m2mm/adversarial_model') is None:
            tf.train.init_from_checkpoint(tf.train.latest_checkpoint('./model_m2mm/source_model/'),
                                          {'source_encoder/': 'target_encoder/'})
        # Create non-streaming (per batch) accuracy
        pred_classes_target = tf.argmax(class_logits_target, axis=1, output_type=tf.int32)
        pred_classes_source = tf.argmax(class_logits_source, axis=1, output_type=tf.int32)
        pred_classes_target_ = tf.argmax(class_logits_target_, axis=1, output_type=tf.int32)
        # Create discriminator labels
        source_adv_label = tf.ones([tf.shape(out_source)[0]], tf.int32)
        target_adv_label = tf.zeros([tf.shape(out_target)[0]], tf.int32)
        # Send encoded vectors through discriminator
        disc_logits_source = discriminator(out_source)
        disc_logits_target = discriminator(out_target)
        # disc_logits_source = tf.Print(disc_logits_source, [tf.argmax(disc_logits_source, axis=1)], 'Source discriminator: ')
        # Calculate losses
        # The generator uses inverted labels as explained in TODO: LINK!!
        loss_gen = tf.losses.sparse_softmax_cross_entropy(logits=disc_logits_target,
                                                          labels=(1 - target_adv_label))
        loss_adv = tf.losses.sparse_softmax_cross_entropy(logits=disc_logits_source,
                                                          labels=source_adv_label) + \
                   tf.losses.sparse_softmax_cross_entropy(logits=disc_logits_target,
                                                          labels=target_adv_label)
        tf.summary.scalar("generator_loss", loss_gen)
        tf.summary.scalar("discriminator_loss", loss_adv)
        tf.identity(loss_gen, 'loss_gen')
        tf.identity(loss_adv, 'loss_adv')
        # Evaluate if in EVAL
        if mode == tf.estimator.ModeKeys.EVAL:
            target_class_acc_ = tf.metrics.accuracy(labels=labels['label_t'],
                                                    predictions=pred_classes_target_,
                                                    name='target_class_encoder_acc')
            source_class_acc = tf.metrics.accuracy(labels=labels['label_s'],
                                                   predictions=pred_classes_source,
                                                   name='source_class_acc_op')
            target_class_acc = tf.metrics.accuracy(labels=labels['label_t'],
                                                   predictions=pred_classes_target,
                                                   name='target_class_acc_op')
            metrics = {'source_class_acc': source_class_acc,
                       'target_class_acc': target_class_acc,
                       'target_class_encoder_acc': target_class_acc_}
            return tf.estimator.EstimatorSpec(
                mode, loss=loss_gen + loss_adv, eval_metric_ops=metrics)
        target_class_acc = utilities.non_streaming_accuracy(pred_classes_target,
                                                            tf.cast(labels['label_t'], tf.int32))
        source_class_acc = utilities.non_streaming_accuracy(pred_classes_source,
                                                            tf.cast(labels['label_s'], tf.int32))
        target_class_acc_enc = utilities.non_streaming_accuracy(pred_classes_target_,
                                                                tf.cast(labels['label_t'], tf.int32))
        tf.identity(target_class_acc, name='target_class_acc')
        tf.identity(source_class_acc, name='source_class_acc')
        tf.identity(target_class_acc_enc, name='target_class_acc_enc')
        # Get the trainable variables
        var_target_encoder = tf.trainable_variables('target_encoder')
        var_discriminator = tf.trainable_variables('discriminator')
        print(var_target_encoder)
        print(var_discriminator)
        optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate, 0.5)
        train_op_gen = optimizer.minimize(loss_gen,
                                          global_step=tf.train.get_global_step(),
                                          var_list=var_target_encoder)
        train_op_adv = optimizer.minimize(loss_adv,
                                          global_step=tf.train.get_global_step(),
                                          var_list=var_discriminator)
        return tf.estimator.EstimatorSpec(mode, loss=loss_gen + loss_adv,
                                          train_op=tf.group(train_op_gen, train_op_adv))


def discriminator(inputs):
    with tf.variable_scope('discriminator', reuse=tf.AUTO_REUSE):
        out_disc = tf.layers.dense(inputs, 500, kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        out_disc = tf.nn.relu(out_disc)
        out_disc = tf.layers.dense(out_disc, 500, kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        out_disc = tf.nn.relu(out_disc)
        out_disc = tf.layers.dense(out_disc, 2, kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
    return out_disc


def lenet_encoder(inputs, scope, trainable=True):
    with tf.variable_scope(scope, reuse=tf.AUTO_REUSE):
        net = tf.layers.conv2d(inputs, 20, kernel_size=5, trainable=trainable,
                               kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        net = tf.nn.relu(net)
        net = tf.layers.max_pooling2d(net, 2, 2)
        net = tf.layers.conv2d(net, 50, kernel_size=5, trainable=trainable,
                               kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        net = tf.nn.relu(net)
        net = tf.layers.max_pooling2d(net, 2, 2)
        net = tf.layers.flatten(net)
        net = tf.layers.dense(net, 120, trainable=trainable,
                              kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        net = tf.nn.relu(net)
        net = tf.layers.dense(net, 84, trainable=trainable,
                              kernel_regularizer=tf.contrib.layers.l2_regularizer(2.5e-5))
        net = tf.nn.tanh(net)
    return net


def main(_):
    # TODO: do not pass source label in target mode (it's not needed!)
    """Main function for Adversarial Discriminative Domain Adaptation - ADDA"""
    tf.reset_default_graph()
    if FLAGS.step == 'source':
        # Pretrain source classifier on SVHN dataset.
        (x_train, y_train), (x_test, y_test) = dp.load_mnist(channel_size=FLAGS.channel_size)
        # Configurations first
        iter_ratio = math.ceil((x_train.shape[0] / FLAGS.batch_size))
        print(iter_ratio)
        # MNIST image shape is 28 x 28
        feature_columns = [tf.feature_column.numeric_column("source", shape=(28, 28, FLAGS.channel_size))]

        # Set up the session config
        session_config = tf.ConfigProto()
        session_config.gpu_options.allow_growth = True

        config = tf.estimator.RunConfig(
            save_checkpoints_steps=int(iter_ratio),
            log_step_count_steps=None,
            session_config=session_config
        )

        # Set up the estimator
        classifier = tf.estimator.Estimator(
            model_fn=estimator_model_fn,
            model_dir="./model_m2mm/source_model",
            params={
                'feature_columns': feature_columns,
                'iter_ratio': iter_ratio,
                'channel_size': FLAGS.channel_size
            },
            config=config
        )
        # Define hooks
        logging_hook = tf.train.LoggingTensorHook(
            tensors={"loss": "loss", "source_class_acc": "source_class_acc"},
            every_n_iter=100)
        # Set up train and eval specs
        train_spec = tf.estimator.TrainSpec(
            input_fn=tf.estimator.inputs.numpy_input_fn({'source': x_train}, {'labels': y_train},
                                                        shuffle=True, batch_size=128,
                                                        num_epochs=FLAGS.total_epochs),
            hooks=[logging_hook]
        )
        eval_spec = tf.estimator.EvalSpec(
            input_fn=tf.estimator.inputs.numpy_input_fn({'source': x_test}, {'labels': y_test},
                                                        shuffle=True, batch_size=128, num_epochs=1),
            steps=None,
            throttle_secs=300
        )
        # Train and evaluate
        tf.estimator.train_and_evaluate(classifier, train_spec, eval_spec)
    if FLAGS.step == 'target':
        # Load MNIST dataset
        (x_train_s, y_train_s), (x_test_s, y_test_s) = dp.load_mnist(channel_size=FLAGS.channel_size, truncate=True)
        # Load MNIST-M dataset
        (x_train_t, y_train_t), (x_test_t, y_test_t) = dp.load_mnistm()
        # Configurations first
        iter_ratio = math.ceil((x_train_s.shape[0] / FLAGS.batch_size))
        print(iter_ratio)
        # MNIST shape is 28 x 28 pixels (transformed to three channels)
        # MNIST-M shape is 32 x 32 pixels with 3 channels
        feature_columns = [tf.feature_column.numeric_column("source", shape=(28, 28, FLAGS.channel_size)),
                           tf.feature_column.numeric_column("target", shape=(32, 32, FLAGS.channel_size))]

        # Set up the session config
        session_config = tf.ConfigProto()
        session_config.gpu_options.allow_growth = True

        config = tf.estimator.RunConfig(
            save_checkpoints_steps=1000,
            log_step_count_steps=None,
            session_config=session_config
        )

        # Set up the estimator
        classifier = tf.estimator.Estimator(
            model_fn=estimator_model_fn,
            model_dir="./model_m2mm/adversarial_model",
            params={
                'feature_columns': feature_columns,
                'iter_ratio': iter_ratio,
                'channel_size': FLAGS.channel_size
            },
            config=config
        )
        # Define hooks
        logging_hook = tf.train.LoggingTensorHook(
            tensors={"loss_gen": "loss_gen", "loss_adv": "loss_adv", "target_class_acc": "target_class_acc",
                     "source_class_acc": "source_class_acc",
                     "target_class_acc_enc": "target_class_acc_enc"},
            every_n_iter=100)
        # Set up train and eval specs
        train_spec = tf.estimator.TrainSpec(
            input_fn=tf.estimator.inputs.numpy_input_fn({'source': x_train_s, 'target': x_train_t},
                                                        {'label_s': y_train_s, 'label_t': y_train_t},
                                                        shuffle=True, batch_size=128,
                                                        num_epochs=FLAGS.total_epochs),
            hooks=[logging_hook]
        )
        eval_spec = tf.estimator.EvalSpec(
            input_fn=tf.estimator.inputs.numpy_input_fn({'source': x_test_s, 'target': x_test_t},
                                                        {'label_s': y_test_s, 'label_t': y_test_t},
                                                        shuffle=True, batch_size=128, num_epochs=1),
            steps=None,
            throttle_secs=300
        )
        # Train and evaluate
        tf.estimator.train_and_evaluate(classifier, train_spec, eval_spec)


if __name__ == '__main__':
    tf.app.run()
