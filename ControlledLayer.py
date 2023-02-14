import torch
import numpy as np
import pytorch_lightning as pl


def spikify(rate):
    return (rate > torch.rand_like(rate))*1


class ControlledLayer(torch.nn.Module):
    def __init__(self, fan_in, fan_out, controller_dim, mode="spiking", leak=0.9, stdp_tau=False):
        super().__init__()

        self.leak = leak
        self.threshold = 1.
        self.fan_out = fan_out
        self.ff = torch.nn.Linear(fan_in, fan_out, bias=False)
        self.fb = torch.nn.Linear(controller_dim, fan_out, bias=False)
        torch.nn.init.eye_(self.fb.weight)  # TODO this is ok with one output
        self.reset()

        assert mode == "spiking" or mode == "rate"
        self.mode = mode
        self.dynamics = self._spiking_dynamics if mode == "spiking" else self._rate_dynamics

        self.stdp_decay = 1 - 1 / stdp_tau if stdp_tau else False
        if self.stdp_decay:
            self.Apre = torch.zeros(fan_in)
            self.Apost = torch.zeros(fan_out)

    def forward(self, inputs, c):
        ff_input = self.ff(inputs)
        fb_input = self.fb(c)

        # LIF dynamics
        self.v += -self.leak * self.v + ff_input + fb_input
        outputs = self.dynamics()

        if self.stdp_decay:
            if self.ff.weight.grad is None:  # TODO
                self.ff.weight.grad = torch.zeros_like(self.ff.weight)
            if self.mode == "rate":
                input_spikes = spikify(inputs)
                output_spikes = spikify(outputs)
            else:
                input_spikes, output_spikes = inputs, outputs
            self.Apre = self.Apre * self.stdp_decay + input_spikes.float()
            self.Apost = self.Apost * self.stdp_decay + output_spikes.float()
            self.ff.weight.grad -= -torch.outer(input_spikes, self.Apost).T + torch.outer(output_spikes, self.Apre)

        return outputs

    def _spiking_dynamics(self):
        spikes = self.v > self.threshold
        self.v[spikes] = 0.

        return spikes.float()

    def _rate_dynamics(self):
        return torch.sigmoid(self.v)

    def reset(self):
        self.v = torch.zeros(self.fan_out)

    @property
    def grad(self):
        return self.ff.weight.grad


class ControlledNetwork(pl.LightningModule):
    def __init__(
        self,
        layers,
        mode="spiking",
        leak=0.9,
        controller_rate=0.1,
        stdp_tau=False,
        controller_precision=0.01,
        target_rates=[0., 1.],
    ):
        super().__init__()
        self.controller_rate = controller_rate
        self.ctr_precision = controller_precision
        self.target_rates = torch.tensor(target_rates).float()

        self.layers = []
        controller_dim = layers[-1]
        self.c = torch.zeros(controller_dim)

        for fan_in, fan_out in zip(layers[:-1], layers[1:]):
            layer = ControlledLayer(
                fan_in, fan_out, controller_dim=controller_dim,
                mode=mode, leak=leak, stdp_tau=stdp_tau)
            self.layers.append(layer)

        self.seq = torch.nn.Sequential(*self.layers)  # so that pytorch registers them

    def forward(self, x, c):
        for layer in self.layers:
            x = layer(x, c)
        return x

    def feedforward(self, x):
        return self(x, torch.zeros_like(self.c))

    def evolve_controller(self, current_output, control_target_rate):
        error = control_target_rate - current_output
        self.c += self.controller_rate * error

    def evolve_to_convergence(self, x, target_rate, control_target_rate):
        self.reset()
        n_iter = 0

        while True:
            output_rate = self(x.float(), self.c).float()  # TODO float()
            self.evolve_controller(output_rate, control_target_rate)
            n_iter += 1
            if n_iter == 1: first_output = output_rate.detach()
            if (output_rate - target_rate).abs().mean() <= self.ctr_precision: break

        return first_output, n_iter

    def parameters(self, recurse: bool = True):
        return (l.ff.weight for l in self.layers)

    def reset(self):
        self.c.zero_()
        for layer in self.layers:
            layer.reset()

    def training_step(self, data, idx):
        optim = self.optimizers().optimizer

        x, y = data
        target = torch.nn.functional.one_hot(y, num_classes=10).squeeze()
        x = x.squeeze()
        control_target_rate = self.target_rates[target]

        # FORWARD, with controller controlling
        first_output, n_iter = self.evolve_to_convergence(
            x, target, control_target_rate)
        optim.step()
        optim.zero_grad()

        ffw_mse = torch.nn.functional.mse_loss(first_output, target)
        self.log("ffw_mse", ffw_mse)
        self.log("time_to_target", n_iter)
