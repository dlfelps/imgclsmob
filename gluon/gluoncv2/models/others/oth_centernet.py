__all__ = ['center_net_resnet18_v1b_voc', 'center_net_resnet18_v1b_dcnv2_voc',
           'center_net_resnet18_v1b_coco', 'center_net_resnet18_v1b_dcnv2_coco',
           'center_net_resnet50_v1b_voc', 'center_net_resnet50_v1b_dcnv2_voc',
           'center_net_resnet50_v1b_coco', 'center_net_resnet50_v1b_dcnv2_coco',
           'center_net_resnet101_v1b_voc', 'center_net_resnet101_v1b_dcnv2_voc',
           'center_net_resnet101_v1b_coco', 'center_net_resnet101_v1b_dcnv2_coco',
           'center_net_dla34_voc', 'center_net_dla34_dcnv2_voc',
           'center_net_dla34_coco', 'center_net_dla34_dcnv2_coco',]


"""CenterNet object detector: Objects as Points, https://arxiv.org/abs/1904.07850"""

import os
import warnings
from collections import OrderedDict

import mxnet as mx
from mxnet.gluon import nn
from mxnet import autograd
from mxnet import gluon


class CenterNetDecoder(gluon.HybridBlock):
    """Decorder for centernet.

    Parameters
    ----------
    topk : int
        Only keep `topk` results.
    scale : float, default is 4.0
        Downsampling scale for the network.

    """
    def __init__(self, topk=100, scale=4.0):
        super(CenterNetDecoder, self).__init__()
        self._topk = topk
        self._scale = scale

    def hybrid_forward(self, F, x, wh, reg):
        """Forward of decoder"""
        _, _, out_h, out_w = x.shape_array().split(num_outputs=4, axis=0)
        scores, indices = x.reshape((0, -1)).topk(k=self._topk, ret_typ='both')
        indices = F.cast(indices, 'int64')
        topk_classes = F.cast(F.broadcast_div(indices, (out_h * out_w)), 'float32')
        topk_indices = F.broadcast_mod(indices, (out_h * out_w))
        topk_ys = F.broadcast_div(topk_indices, out_w)
        topk_xs = F.broadcast_mod(topk_indices, out_w)
        center = reg.transpose((0, 2, 3, 1)).reshape((0, -1, 2))
        wh = wh.transpose((0, 2, 3, 1)).reshape((0, -1, 2))
        batch_indices = F.cast(F.arange(256).slice_like(
            center, axes=(0)).expand_dims(-1).tile(reps=(1, self._topk)), 'int64')
        reg_xs_indices = F.zeros_like(batch_indices, dtype='int64')
        reg_ys_indices = F.ones_like(batch_indices, dtype='int64')
        reg_xs = F.concat(batch_indices, topk_indices, reg_xs_indices, dim=0).reshape((3, -1))
        reg_ys = F.concat(batch_indices, topk_indices, reg_ys_indices, dim=0).reshape((3, -1))
        xs = F.cast(F.gather_nd(center, reg_xs).reshape((-1, self._topk)), 'float32')
        ys = F.cast(F.gather_nd(center, reg_ys).reshape((-1, self._topk)), 'float32')
        topk_xs = F.cast(topk_xs, 'float32') + xs
        topk_ys = F.cast(topk_ys, 'float32') + ys
        w = F.cast(F.gather_nd(wh, reg_xs).reshape((-1, self._topk)), 'float32')
        h = F.cast(F.gather_nd(wh, reg_ys).reshape((-1, self._topk)), 'float32')
        half_w = w / 2
        half_h = h / 2
        results = [topk_xs - half_w, topk_ys - half_h, topk_xs + half_w, topk_ys + half_h]
        results = F.concat(*[tmp.expand_dims(-1) for tmp in results], dim=-1)
        return topk_classes, scores, results * self._scale


class CenterNet(nn.HybridBlock):
    """Objects as Points. https://arxiv.org/abs/1904.07850v2

    Parameters
    ----------
    base_network : mxnet.gluon.nn.HybridBlock
        The base feature extraction network.
    heads : OrderedDict
        OrderedDict with specifications for each head.
        For example: OrderedDict([
            ('heatmap', {'num_output': len(classes), 'bias': -2.19}),
            ('wh', {'num_output': 2}),
            ('reg', {'num_output': 2})
            ])
    classes : list of str
        Category names.
    head_conv_channel : int, default is 0
        If > 0, will use an extra conv layer before each of the real heads.
    scale : float, default is 4.0
        The downsampling ratio of the entire network.
    topk : int, default is 100
        Number of outputs .
    flip_test : bool
        Whether apply flip test in inference (training mode not affected).
    nms_thresh : float, default is 0.
        Non-maximum suppression threshold. You can specify < 0 or > 1 to disable NMS.
        By default nms is disabled.
    nms_topk : int, default is 400
        Apply NMS to top k detection results, use -1 to disable so that every Detection
         result is used in NMS.
    post_nms : int, default is 100
        Only return top `post_nms` detection results, the rest is discarded. The number is
        based on COCO dataset which has maximum 100 objects per image. You can adjust this
        number if expecting more objects. You can use -1 to return all detections.

    """
    def __init__(self, base_network, heads, classes,
                 head_conv_channel=0, scale=4.0, topk=100, flip_test=False,
                 nms_thresh=0, nms_topk=400, post_nms=100, **kwargs):
        if 'norm_layer' in kwargs:
            kwargs.pop('norm_layer')
        if 'norm_kwargs' in kwargs:
            kwargs.pop('norm_kwargs')
        super(CenterNet, self).__init__(**kwargs)
        assert isinstance(heads, OrderedDict), \
            "Expecting heads to be a OrderedDict per head, given {}" \
            .format(type(heads))
        self.classes = classes
        self.topk = topk
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        post_nms = min(post_nms, topk)
        self.post_nms = post_nms
        self.scale = scale
        self.flip_test = flip_test
        with self.name_scope():
            self.base_network = base_network
            self.heatmap_nms = nn.MaxPool2D(pool_size=3, strides=1, padding=1)
            self.decoder = CenterNetDecoder(topk=topk, scale=scale)
            self.heads = nn.HybridSequential('heads')
            for name, values in heads.items():
                head = nn.HybridSequential(name)
                num_output = values['num_output']
                bias = values.get('bias', 0.0)
                weight_initializer = mx.init.Normal(0.001) if bias == 0 else mx.init.Xavier()
                if head_conv_channel > 0:
                    head.add(nn.Conv2D(
                        head_conv_channel, kernel_size=3, padding=1, use_bias=True,
                        weight_initializer=weight_initializer, bias_initializer='zeros'))
                    head.add(nn.Activation('relu'))
                head.add(nn.Conv2D(num_output, kernel_size=1, strides=1, padding=0, use_bias=True,
                                   weight_initializer=weight_initializer,
                                   bias_initializer=mx.init.Constant(bias)))

                self.heads.add(head)

    @property
    def num_classes(self):
        """Return number of foreground classes.

        Returns
        -------
        int
            Number of foreground classes

        """
        return len(self.classes)

    def set_nms(self, nms_thresh=0, nms_topk=400, post_nms=100):
        """Set non-maximum suppression parameters.

        Parameters
        ----------
        nms_thresh : float, default is 0.
            Non-maximum suppression threshold. You can specify < 0 or > 1 to disable NMS.
            By default NMS is disabled.
        nms_topk : int, default is 400
            Apply NMS to top k detection results, use -1 to disable so that every Detection
             result is used in NMS.
        post_nms : int, default is 100
            Only return top `post_nms` detection results, the rest is discarded. The number is
            based on COCO dataset which has maximum 100 objects per image. You can adjust this
            number if expecting more objects. You can use -1 to return all detections.

        Returns
        -------
        None

        """
        self._clear_cached_op()
        self.nms_thresh = nms_thresh
        self.nms_topk = nms_topk
        post_nms = min(post_nms, self.nms_topk)
        self.post_nms = post_nms

    def reset_class(self, classes, reuse_weights=None):
        """Reset class categories and class predictors.

        Parameters
        ----------
        classes : iterable of str
            The new categories. ['apple', 'orange'] for example.
        reuse_weights : dict
            A {new_integer : old_integer} or mapping dict or {new_name : old_name} mapping dict,
            or a list of [name0, name1,...] if class names don't change.
            This allows the new predictor to reuse the
            previously trained weights specified.

        """
        raise NotImplementedError("Not yet implemented, please wait for future updates.")

    def hybrid_forward(self, F, x):
        # pylint: disable=arguments-differ
        """Hybrid forward of center net"""
        y = self.base_network(x)
        out = [head(y) for head in self.heads]
        out[0] = F.sigmoid(out[0])
        if autograd.is_training():
            out[0] = F.clip(out[0], 1e-4, 1 - 1e-4)
            return tuple(out)
        if self.flip_test:
            y_flip = self.base_network(x.flip(axis=3))
            out_flip = [head(y_flip) for head in self.heads]
            out_flip[0] = F.sigmoid(out_flip[0])
            out[0] = (out[0] + out_flip[0].flip(axis=3)) * 0.5
            out[1] = (out[1] + out_flip[1].flip(axis=3)) * 0.5
        heatmap = out[0]
        keep = F.broadcast_equal(self.heatmap_nms(heatmap), heatmap)
        results = self.decoder(keep * heatmap, out[1], out[2])
        return results


def get_center_net(name, dataset, pretrained=False, ctx=mx.cpu(),
                   root=os.path.join('~', '.mxnet', 'models'), **kwargs):
    """Get a center net instance.

    Parameters
    ----------
    name : str or None
        Model name, if `None` is used, you must specify `features` to be a `HybridBlock`.
    dataset : str
        Name of dataset. This is used to identify model name because models trained on
        different datasets are going to be very different.
    pretrained : bool or str
        Boolean value controls whether to load the default pretrained weights for model.
        String value represents the hashtag for a certain version of pretrained weights.
    ctx : mxnet.Context
        Context such as mx.cpu(), mx.gpu(0).
    root : str
        Model weights storing path.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    # pylint: disable=unused-variable
    net = CenterNet(**kwargs)
    if pretrained:
        from ..model_store import get_model_file
        full_name = '_'.join(('center_net', name, dataset))
        net.load_parameters(get_model_file(full_name, tag=pretrained, root=root), ctx=ctx)
    else:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            net.initialize()
        for v in net.collect_params().values():
            try:
                v.reset_ctx(ctx)
            except ValueError:
                pass
    return net


def center_net_resnet18_v1b_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet18_v1b base network on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet18_v1b_deconv
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet18_v1b_deconv
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet18_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}),  # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet18_v1b', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet18_v1b_dcnv2_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet18_v1b base network with deformable v2 conv layers on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet18_v1b_deconv_dcnv2
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet18_v1b_deconv_dcnv2
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet18_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet18_v1b_dcnv2', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet18_v1b_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet18_v1b base network on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet18_v1b_deconv
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet18_v1b_deconv
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet18_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet18_v1b', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet18_v1b_dcnv2_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet18_v1b base network with deformable v2 conv layer on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet18_v1b_deconv_dcnv2
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet18_v1b_deconv_dcnv2
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet18_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet18_v1b_dcnv2', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet50_v1b_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet50_v1b base network on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet50_v1b_deconv
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet50_v1b_deconv
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet50_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet50_v1b', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet50_v1b_dcnv2_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet50_v1b base network with deformable conv layers on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet50_v1b_deconv_dcnv2
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet50_v1b_deconv_dcnv2
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet50_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet50_v1b_dcnv2', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet50_v1b_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet50_v1b base network on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet50_v1b_deconv
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet50_v1b_deconv
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet50_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet50_v1b', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet50_v1b_dcnv2_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet50_v1b base network with deformable v2 conv layers on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet50_v1b_deconv_dcnv2
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet50_v1b_deconv_dcnv2
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet50_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet50_v1b_dcnv2', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet101_v1b_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet101_v1b_deconv
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet101_v1b_deconv
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet101_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet101_v1b', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet101_v1b_dcnv2_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network with deformable conv layers on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet101_v1b_deconv_dcnv2
    from gluoncv.data import VOCDetection
    # from .deconv_resnet import resnet101_v1b_deconv_dcnv2
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet101_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet101_v1b_dcnv2', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet101_v1b_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet101_v1b_deconv
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet101_v1b_deconv
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet101_v1b_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet101_v1b', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_resnet101_v1b_dcnv2_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network with deformable v2 conv layers on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_resnet import resnet101_v1b_deconv_dcnv2
    from gluoncv.data import COCODetection
    # from .deconv_resnet import resnet101_v1b_deconv_dcnv2
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = resnet101_v1b_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('resnet101_v1b_dcnv2', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_dla34_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with dla34 base network on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_dla import dla34_deconv
    from gluoncv.data import VOCDetection
    # from .deconv_dla import dla34_deconv
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = dla34_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('dla34', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_dla34_dcnv2_voc(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network with deformable conv layers on voc dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_dla import dla34_deconv_dcnv2
    from gluoncv.data import VOCDetection
    # from .deconv_dla import dla34_deconv_dcnv2
    # from ...data import VOCDetection
    classes = VOCDetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = dla34_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}),  # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('dla34', 'voc', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_dla34_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_dla import dla34_deconv
    from gluoncv.data import COCODetection
    # from .deconv_dla import dla34_deconv
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = dla34_deconv(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('dla34', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def center_net_dla34_dcnv2_coco(pretrained=False, pretrained_base=True, **kwargs):
    """Center net with resnet101_v1b base network with deformable v2 conv layers on coco dataset.

    Parameters
    ----------
    classes : iterable of str
        Names of custom foreground classes. `len(classes)` is the number of foreground classes.
    pretrained_base : bool or str, optional, default is True
        Load pretrained base network, the extra layers are randomized.

    Returns
    -------
    HybridBlock
        A CenterNet detection network.

    """
    from gluoncv.model_zoo.center_net.deconv_dla import dla34_deconv_dcnv2
    from gluoncv.data import COCODetection
    # from .deconv_dla import dla34_deconv_dcnv2
    # from ...data import COCODetection
    classes = COCODetection.CLASSES
    pretrained_base = False if pretrained else pretrained_base
    base_network = dla34_deconv_dcnv2(pretrained=pretrained_base, **kwargs)
    heads = OrderedDict([
        ('heatmap', {'num_output': len(classes), 'bias': -2.19}), # use bias = -log((1 - 0.1) / 0.1)
        ('wh', {'num_output': 2}),
        ('reg', {'num_output': 2})
        ])
    return get_center_net('dla34', 'coco', base_network=base_network, heads=heads,
                          head_conv_channel=64, pretrained=pretrained, classes=classes,
                          scale=4.0, topk=40, **kwargs)


def _test():
    import numpy as np
    import mxnet as mx

    in_size = (480, 480)
    pretrained = False

    models = [
        center_net_resnet18_v1b_voc,
        # center_net_resnet18_v1b_dcnv2_voc,
        center_net_resnet18_v1b_coco,
        # center_net_resnet18_v1b_dcnv2_coco,
        center_net_resnet50_v1b_voc,
        # center_net_resnet50_v1b_dcnv2_voc,
        center_net_resnet50_v1b_coco,
        # center_net_resnet50_v1b_dcnv2_coco,
        center_net_resnet101_v1b_voc,
        # center_net_resnet101_v1b_dcnv2_voc,
        center_net_resnet101_v1b_coco,
        # center_net_resnet101_v1b_dcnv2_coco,
        # center_net_dla34_voc,
        # center_net_dla34_dcnv2_voc,
        # center_net_dla34_coco,
        # center_net_dla34_dcnv2_coco,
    ]

    for model in models:

        net = model(pretrained=pretrained)

        ctx = mx.cpu()
        if not pretrained:
            net.initialize(ctx=ctx)

        x = mx.nd.zeros((1, 3, in_size[0], in_size[1]), ctx=ctx)
        y = net(x)

        # net.hybridize()
        net_params = net.collect_params()
        weight_count = 0
        for param in net_params.values():
            if (param.shape is None) or (not param._differentiable):
                continue
            weight_count += np.prod(param.shape)
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != center_net_resnet18_v1b_voc or weight_count == 14215640)
        assert (model != center_net_resnet18_v1b_coco or weight_count == 14219540)
        assert (model != center_net_resnet50_v1b_voc or weight_count == 30086104)
        assert (model != center_net_resnet50_v1b_coco or weight_count == 30090004)
        assert (model != center_net_resnet101_v1b_voc or weight_count == 49078232)
        assert (model != center_net_resnet101_v1b_coco or weight_count == 49082132)
        # assert (model != center_net_dla34_voc or weight_count == 14219540)
        # assert (model != center_net_dla34_coco or weight_count == 14219540)


if __name__ == "__main__":
    _test()
