import megengine as mge
from megengine import module as M
import megengine.functional as F
from model.warplayer import warp
from model.refine import *

def deconv(in_planes, out_planes, kernel_size=4, stride=2, padding=1):
    return M.Sequential(
        M.ConvTranspose2d(in_channels=in_planes, out_channels=out_planes, kernel_size=4, stride=2, padding=1),
        M.PReLU(out_planes)
    )

def conv(in_planes, out_planes, kernel_size=3, stride=1, padding=1, dilation=1):
    return M.Sequential(
        M.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride,
                  padding=padding, dilation=dilation, bias=True),
        M.PReLU(out_planes)
    )

class IFBlock(M.Module):
    def __init__(self, in_planes, c=64):
        super(IFBlock, self).__init__()
        self.conv0 = M.Sequential(
            conv(in_planes, c//2, 3, 2, 1),
            conv(c//2, c, 3, 2, 1),
            )
        self.convblock = M.Sequential(
            conv(c, c),
            conv(c, c),
            conv(c, c),
            conv(c, c),
            conv(c, c),
            conv(c, c),
            conv(c, c),
            conv(c, c),
        )
        self.lastconv = M.ConvTranspose2d(c, 5, 4, 2, 1)

    def forward(self, x, flow, scale):
        if scale != 1:
            x = F.nn.interpolate(x, scale_factor = 1. / scale, mode="bilinear", align_corners=False)
        if flow is not None:
            flow = F.nn.interpolate(flow, scale_factor = 1. / scale, mode="bilinear", align_corners=False) * 1. / scale
            x = F.concat((x, flow), 1)
        x = self.conv0(x)
        x = self.convblock(x) + x
        tmp = self.lastconv(x)
        tmp = F.nn.interpolate(tmp, scale_factor = scale * 2, mode="bilinear", align_corners=False)
        flow = tmp[:, :4] * scale * 2
        mask = tmp[:, 4:5]
        return flow, mask
    
class IFNet(M.Module):
    def __init__(self):
        super(IFNet, self).__init__()
        self.block0 = IFBlock(6, c=240)
        self.block1 = IFBlock(13+4, c=150)
        self.block2 = IFBlock(13+4, c=90)
        self.block_tea = IFBlock(16+4, c=90)
        self.contextnet = Contextnet()
        self.unet = Unet()

    def forward(self, x, scale=[4,2,1], timestep=0.5):
        img0 = x[:, :3]
        img1 = x[:, 3:6]
        gt = x[:, 6:] # In inference time, gt is None
        flow_list = []
        merged = []
        mask_list = []
        warped_img0 = img0
        warped_img1 = img1
        flow = None 
        loss_distill = 0
        stu = [self.block0, self.block1, self.block2]
        for i in range(3):
            if flow is not None:
                flow_d, mask_d = stu[i](F.concat((img0, img1, warped_img0, warped_img1, mask), 1), flow, scale=scale[i])
                flow = flow + flow_d
                mask = mask + mask_d
            else:
                flow, mask = stu[i](F.concat((img0, img1), 1), None, scale=scale[i])
            mask_list.append(F.nn.sigmoid(mask))
            flow_list.append(flow)
            warped_img0 = warp(img0, flow[:, :2])
            warped_img1 = warp(img1, flow[:, 2:4])
            merged_student = (warped_img0, warped_img1)
            merged.append(merged_student)
        if gt.shape[1] == 3:
            flow_d, mask_d = self.block_tea(F.concat((img0, img1, warped_img0, warped_img1, mask, gt), 1), flow, scale=1)
            flow_teacher = flow + flow_d
            warped_img0_teacher = warp(img0, flow_teacher[:, :2])
            warped_img1_teacher = warp(img1, flow_teacher[:, 2:4])
            mask_teacher = F.nn.sigmoid(mask + mask_d)
            merged_teacher = warped_img0_teacher * mask_teacher + warped_img1_teacher * (1 - mask_teacher)
        else:
            flow_teacher = None
            merged_teacher = None
        for i in range(3):
            merged[i] = merged[i][0] * mask_list[i] + merged[i][1] * (1 - mask_list[i])
            if gt.shape[1] == 3:
                loss_mask = (F.abs(merged[i] - gt).mean(1, True) > F.abs(merged_teacher - gt).mean(1, True) + 0.01).astype("float32").detach()
                loss_distill += (F.abs(flow_teacher.detach() - flow_list[i]) * loss_mask).mean()
                
        c0 = self.contextnet(img0, flow[:, :2])
        c1 = self.contextnet(img1, flow[:, 2:4])
        tmp = self.unet(img0, img1, warped_img0, warped_img1, mask, flow, c0, c1)
        res = tmp[:, :3] * 2 - 1
        merged[2] = F.clip(merged[2] + res, 0, 1)
        return flow_list, mask_list[2], merged, flow_teacher, merged_teacher, loss_distill
