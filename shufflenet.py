import tensorflow as tf
import numpy as np

SHUFFLENET_MEAN = [103.939, 116.779, 123.68]
NORMALIZER = 0.017

class Shufflenet:
	def __init__(self):
		self.trained_model = np.load('../ShuffleNetV1-1x-8g.npz', encoding = 'latin1')
		print("npz file loaded")
#		for x in self.trained_model.files:
#			print(x + " " + str(self.trained_model[x].shape))

	def pw_gconv(self, activations, stage, block, layer, num_groups):
		layer_name = str(stage) + '/' + str(block) + '/' + str(layer) + '/W:0'
		kernels = self.trained_model[layer_name]
		ch_per_group = activations.shape[3] // num_groups
		act_split = tf.split(activations, num_or_size_splits = num_groups, axis = 3)
		kernels_split = tf.split(kernels, num_or_size_splits = num_groups, axis = 3)
		convs = []
		for grp in range(0, num_groups):
			convs.append(tf.nn.conv2d(act_split[grp], kernels_split[grp], padding = 'SAME', strides = [1, 1, 1, 1], data_format = 'NHWC'))
		return tf.concat(convs, axis = 3)

	# Depth-wise convolution: Assumes input activations are of shape [B, H, W, C], Assumes depthwise multiplier of 1
	def dw_conv(self, activations, stage, block, padding = 'SAME', stride = 1):
		inp_ch = activations.shape[3]
		act_shape = activations.shape
		layer_name = str(stage) + '/' + str(block) + '/dconv/W:0'
		kernels = self.trained_model[layer_name]
		kernel_size = kernels.shape[0]
		conv_result = tf.nn.depthwise_conv2d(activations, kernels, [1, stride, stride, 1], padding = padding, data_format = 'NHWC')
		return conv_result

	def batch_normalization(self, activations, stage, block, layer):
		layer_name = str(stage) + '/' + str(block) + '/' if stage is not '' else ''
		layer_name = layer_name + 'conv1/bn/' if layer == 'conv1' else layer_name + layer+'_bn/'
		mean = self.trained_model[layer_name + 'mean/EMA:0']
		variance = self.trained_model[layer_name + 'variance/EMA:0']
		gamma = self.trained_model[layer_name + 'gamma:0']
		beta = self.trained_model[layer_name + 'beta:0']
		bn_out = tf.nn.batch_normalization(activations, mean.reshape(1, 1, 1, mean.shape[0]), variance.reshape(1, 1, 1, variance.shape[0]), beta.reshape(1, 1, 1, beta.shape[0]), gamma.reshape(1, 1, 1, gamma.shape[0]), 0.0001)
		return bn_out

	def channel_shuffle(self, activations, num_groups):
		activations = tf.transpose(activations, perm = [0, 3, 1, 2])
		in_shape = activations.get_shape().as_list()
		in_channel = in_shape[1]
		l = tf.reshape(activations, [-1, in_channel // num_groups, num_groups] + in_shape[-2:])
		l = tf.transpose(l, [0, 2, 1, 3, 4])
		l = tf.reshape(l, [-1, in_channel] + in_shape[-2:])
		l = tf.transpose(l, perm = [0, 2, 3, 1])
		return l

	def shufflenet_unit(self, activations, stage, block, stride, num_groups):
		residual = activations
		num_split = num_groups if activations.shape[3] > 24 else 1
		pwgconv1 = self.pw_gconv(activations, stage, block, 'conv1', num_split)
		bnconv1 = self.batch_normalization(pwgconv1, stage, block, 'conv1')
		reluconv1 = tf.nn.relu(bnconv1)
		ch_sh = self.channel_shuffle(reluconv1, num_groups)
		dconv = self.dw_conv(ch_sh, stage, block, padding = 'SAME', stride = stride)
		bndconv = self.batch_normalization(dconv, stage, block, 'dconv')
		pwgconv2 = self.pw_gconv(bndconv, stage, block, 'conv2', num_groups)
		bnconv2 = self.batch_normalization(pwgconv2, stage, block, 'conv2')

		if stride == 1:
			return tf.nn.relu(bnconv2 + residual)
		else:
			residual = tf.nn.avg_pool(residual, [1, 3, 3, 1], strides = [1, 2, 2, 1], padding = 'SAME', data_format = 'NHWC')
			return tf.concat([residual, tf.nn.relu(bnconv2)], axis = 3)

	def shufflenet_stage(self, activations, stage, repeat, num_groups):
		first_block = self.shufflenet_unit(activations, stage, 'block0', stride = 2, num_groups = 8)
		res = first_block
		for b in range(1, repeat+1):
			res = self.shufflenet_unit(res, stage, 'block' + str(b), stride = 1, num_groups = 8)
		return res

	def shufflenet_stage1(self, activations):
		kernels = self.trained_model['conv1/W:0']
		res = tf.nn.conv2d(activations, kernels, padding = 'SAME', strides = [1, 2, 2, 1], data_format = 'NHWC')
		res = self.batch_normalization(res, '', '', 'conv1')
		res = tf.nn.max_pool(res, [1, 3, 3, 1], strides = [1, 2, 2, 1], padding = 'SAME', data_format = 'NHWC')
		return res

	def fc_layer(self, activations):
		layer_name = 'linear'
		weights = self.trained_model[layer_name + '/W:0']
		biases = self.trained_model[layer_name + '/b:0']
		flattened_out = tf.contrib.layers.flatten(activations)
		return tf.nn.bias_add(tf.matmul(flattened_out, weights), biases)

	def build(self, image):
		red, green, blue = tf.split(axis=3, num_or_size_splits=3, value=image)
		bgr = tf.concat(axis=3, values=[(blue - SHUFFLENET_MEAN[0])*NORMALIZER, (green - SHUFFLENET_MEAN[1])*NORMALIZER, (red - SHUFFLENET_MEAN[2])*NORMALIZER])
		stage1 = self.shufflenet_stage1(bgr)
		stage2 = self.shufflenet_stage(stage1, 'stage2', repeat = 3, num_groups = 8)
		stage3 = self.shufflenet_stage(stage2, 'stage3', repeat = 7, num_groups = 8)
		stage4 = self.shufflenet_stage(stage3, 'stage4', repeat = 3, num_groups = 8)
		g_pool = tf.nn.avg_pool(stage4, [1, 7, 7, 1], strides = [1, 1, 1, 1], padding = 'VALID', data_format = 'NHWC')
		logits = self.fc_layer(g_pool)
		logits = tf.nn.softmax(logits)
		return logits