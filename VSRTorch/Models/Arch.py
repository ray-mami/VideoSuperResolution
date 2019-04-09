#  Copyright (c): Wenyi Tang 2017-2019.
#  Author: Wenyi Tang
#  Email: wenyi.tang@intel.com
#  Update Date: 2019/4/3 下午5:10

import torch
import torch.nn as nn
import torch.nn.functional as F

from VSR.Util.Utility import to_list


def _get_act(name, *args, inplace=False):
  if name.lower() == 'relu':
    return nn.ReLU(inplace)
  if name.lower() in ('lrelu', 'leaky', 'leakyrelu'):
    return nn.LeakyReLU(*args, inplace=inplace)
  if name.lower() == 'prelu':
    return nn.PReLU(*args)

  raise TypeError("Unknown activation name!")


class Rdb(nn.Module):
  def __init__(self, channels, depth=3, scaling=1.0, name='Rdb', **kwargs):
    super(Rdb, self).__init__()
    self.name = name
    self.depth = depth
    self.scaling = scaling
    in_c, out_c = to_list(channels, 2)
    ks = kwargs.get('kernel_size', 3)
    stride = kwargs.get('stride', 1)
    padding = kwargs.get('padding', ks // 2)
    dilation = kwargs.get('dilation', 1)
    group = kwargs.get('group', 1)
    bias = kwargs.get('bias', True)
    for i in range(depth):
      conv = nn.Conv2d(
        in_c + out_c * i, out_c, ks, stride, padding, dilation, group, bias)
      if i < depth - 1:  # no activation after last layer
        conv = nn.Sequential(conv, nn.ReLU(True))
      setattr(self, f'conv_{i}', conv)

  def forward(self, inputs):
    fl = [inputs]
    for i in range(self.depth):
      conv = getattr(self, f'conv_{i}')
      fl.append(conv(torch.cat(fl, dim=1)))
    return fl[-1] * self.scaling + inputs

  def extra_repr(self):
    return f"{self.name}: depth={self.depth}, scaling={self.scaling}"


class Rcab(nn.Module):
  def __init__(self, channels, ratio=16, name='RCAB', **kwargs):
    super(Rcab, self).__init__()
    self.name = name
    self.ratio = ratio
    in_c, out_c = to_list(channels, 2)
    ks = kwargs.get('kernel_size', 3)
    padding = kwargs.get('padding', ks // 2)
    group = kwargs.get('group', 1)
    bias = kwargs.get('bias', True)
    self.c1 = nn.Sequential(
      nn.Conv2d(in_c, out_c, ks, 1, padding, 1, group, bias),
      nn.ReLU(True))
    self.c2 = nn.Conv2d(out_c, out_c, ks, 1, padding, 1, group, bias)
    self.c3 = nn.Sequential(
      nn.Conv2d(out_c, out_c // ratio, 1, groups=group, bias=bias),
      nn.ReLU(True))
    self.c4 = nn.Sequential(
      nn.Conv2d(out_c // ratio, in_c, 1, groups=group, bias=bias),
      nn.Sigmoid())
    self.pooling = nn.AdaptiveAvgPool2d(1)

  def forward(self, inputs):
    x = self.c1(inputs)
    y = self.c2(x)
    x = self.pooling(y)
    x = self.c3(x)
    x = self.c4(x)
    y = x * y
    return inputs + y

  def extra_repr(self):
    return f"{self.name}: ratio={self.ratio}"


class CascadeRdn(nn.Module):
  def __init__(self, channels, depth=3, use_ca=False, name='CascadeRdn',
               **kwargs):
    super(CascadeRdn, self).__init__()
    self.name = name
    self.depth = to_list(depth, 2)
    self.ca = use_ca
    in_c, out_c = to_list(channels, 2)
    for i in range(self.depth[0]):
      setattr(self, f'conv11_{i}', nn.Conv2d(in_c + out_c * (i + 1), out_c, 1))
      setattr(self, f'rdn_{i}', Rdb(channels, self.depth[1], **kwargs))
      if use_ca:
        setattr(self, f'rcab_{i}', Rcab(channels))

  def forward(self, inputs):
    fl = [inputs]
    x = inputs
    for i in range(self.depth[0]):
      rdn = getattr(self, f'rdn_{i}')
      x = rdn(x)
      if self.ca:
        rcab = getattr(self, f'rcab_{i}')
        x = rcab(x)
      fl.append(x)
      c11 = getattr(self, f'conv11_{i}')
      x = c11(torch.cat(fl, dim=1))

    return x

  def extra_repr(self):
    return f"{self.name}: depth={self.depth}, ca={self.ca}"


class Activation(nn.Module):
  def __init__(self, name, *args, **kwargs):
    super(Activation, self).__init__()
    self.name = name.lower()
    in_place = kwargs.get('in_place', True)
    if self.name == 'relu':
      self.f = nn.ReLU(in_place)
    elif self.name in ('lrelu', 'leaky', 'leakyrelu'):
      self.f = nn.LeakyReLU(*args, inplace=in_place)
    elif self.name == 'tanh':
      self.f = nn.Tanh()
    elif self.name == 'sigmoid':
      self.f = nn.Sigmoid()

  def forward(self, x):
    return self.f(x)


class _UpsampleNearest(nn.Module):
  def __init__(self, scale):
    super(_UpsampleNearest, self).__init__()
    self.scale = scale

  def forward(self, x, scale=None):
    scale = scale or self.scale
    return F.interpolate(x, scale_factor=scale, align_corners=False)


class _UpsampleLinear(nn.Module):
  def __init__(self, scale):
    super(_UpsampleLinear, self).__init__()
    self._mode = ('linear', 'bilinear', 'trilinear')
    self.scale = scale

  def forward(self, x, scale=None):
    scale = scale or self.scale
    mode = self._mode[x.dim() - 3]
    return F.interpolate(x, scale_factor=scale, mode=mode, align_corners=False)


class Upsample(nn.Module):
  def __init__(self, channel, scale, method='ps', name='Upsample', **kwargs):
    super(Upsample, self).__init__()
    self.name = name
    self.channel = channel
    self.scale = scale
    self.method = method.lower()
    self.kernel_size = kwargs.get('kernel_size', 3)

    _allowed_methods = ('ps', 'nearest', 'deconv', 'linear')
    assert self.method in _allowed_methods
    act = kwargs.get('activation')

    samplers = []
    while scale > 1:
      if scale % 2 == 1 or scale == 2:
        samplers.append(self.upsampler(self.method, scale))
        break
      else:
        samplers.append(self.upsampler(self.method, 2, Activation(act)))
        scale //= 2
    self.body = nn.Sequential(*samplers)

  def upsampler(self, method, scale, activation=None):
    body = []
    k = self.kernel_size
    if method == 'ps':
      p = k // 2  # padding
      s = 1  # strides
      body = [nn.Conv2d(self.channel, self.channel * scale * scale, k, s, p),
              nn.PixelShuffle(scale)]
      if activation:
        body.insert(1, activation)
    if method == 'deconv':
      q = k % 2  # output padding
      p = (k + q) // 2 - 1  # padding
      s = scale  # strides
      body = [nn.ConvTranspose2d(self.channel, self.channel, k, s, p, q)]
      if activation:
        body.insert(1, activation)
    if method == 'nearest':
      body = [_UpsampleNearest(scale)]
      if activation:
        body.insert(1, activation)
    if method == 'linear':
      body = [_UpsampleLinear(scale)]
      if activation:
        body.insert(1, activation)
    return nn.Sequential(*body)

  def forward(self, inputs):
    return self.body(inputs)

  def extra_repr(self):
    return f"{self.name}: scale={self.scale}"


class SpaceToDim(nn.Module):
  def __init__(self, scale_factor, dims=(-2, -1), dim=0):
    super(SpaceToDim, self).__init__()
    self.scale_factor = scale_factor
    self.dims = dims
    self.dim = dim

  def forward(self, x):
    _shape = list(x.shape)
    shape = _shape.copy()
    dims = [x.dim() + self.dims[0] if self.dims[0] < 0 else self.dims[0],
            x.dim() + self.dims[1] if self.dims[1] < 0 else self.dims[1]]
    dims = [max(abs(dims[0]), abs(dims[1])),
            min(abs(dims[0]), abs(dims[1]))]
    if self.dim in dims:
      raise RuntimeError("Integrate dimension can't be space dimension!")
    shape[dims[0]] //= self.scale_factor
    shape[dims[1]] //= self.scale_factor
    shape.insert(dims[0] + 1, self.scale_factor)
    shape.insert(dims[1] + 1, self.scale_factor)
    dim = self.dim if self.dim < dims[1] else self.dim + 1
    dim = dim if dim <= dims[0] else dim + 1
    x = x.reshape(*shape)
    perm = [dim, dims[1] + 1, dims[0] + 2]
    perm = [i for i in range(min(perm))] + perm
    perm.extend((i for i in range(x.dim()) if i not in perm))
    x = x.permute(*perm)
    shape = _shape
    shape[self.dim] *= self.scale_factor ** 2
    shape[self.dims[0]] //= self.scale_factor
    shape[self.dims[1]] //= self.scale_factor
    return x.reshape(*shape)

  def extra_repr(self):
    return f'scale_factor={self.scale_factor}'


class SpaceToDepth(nn.Module):
  def __init__(self, block_size):
    super(SpaceToDepth, self).__init__()
    self.body = SpaceToDim(block_size, dim=1)

  def forward(self, x):
    return self.body(x)


class SpaceToBatch(nn.Module):
  def __init__(self, block_size):
    super(SpaceToBatch, self).__init__()
    self.body = SpaceToDim(block_size, dim=0)

  def forward(self, x):
    return self.body(x)
