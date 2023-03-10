from functools import partial
from warnings import warn

import numpy as np
import torch
from nnfabrik.utility.nn_helpers import set_random_seed
from tqdm import tqdm

import neuralpredictors.measures.zero_inflated_losses as losses
from neuralpredictors.measures.modules import PoissonLoss
from neuralpredictors.training import LongCycler, MultipleObjectiveTracker, early_stopping

from ..utility import measures


def standard_trainer(
    model,
    dataloaders,
    seed,
    loss_function="PoissonLoss",
    avg_loss=False,
    scale_loss=True,
    stop_function="get_correlations",
    loss_accum_batch_n=None,
    device="cuda",
    verbose=True,
    interval=1,
    patience=5,
    epoch=0,
    lr_init=0.005,
    max_iter=200,
    maximize=True,
    tolerance=1e-6,
    restore_best=True,
    lr_decay_steps=3,
    lr_decay_factor=0.3,
    min_lr=0.0001,
    cb=None,
    track_training=False,
    return_test_score=False,
    detach_core=False,
    **kwargs
):
    """

    Args:
        model: model to be trained
        dataloaders: dataloaders containing the data to train the model with
        seed: random seed
        avg_loss: whether to average (or sum) the loss over a batch
        scale_loss: whether to scale the loss according to the size of the dataset
        loss_function: loss function to use
        stop_function: the function (metric) that is used to determine the end of the training in early stopping
        loss_accum_batch_n: number of batches to accumulate the loss over
        device: device to run the training on
        verbose: whether to print out a message for each optimizer step
        interval: interval at which objective is evaluated to consider early stopping
        patience: number of times the objective is allowed to not become better before the iterator terminates
        epoch: starting epoch
        lr_init: initial learning rate
        max_iter: maximum number of training iterations
        maximize: whether to maximize or minimize the objective function
        tolerance: tolerance for early stopping
        restore_best: whether to restore the model to the best state after early stopping
        lr_decay_steps: how many times to decay the learning rate after no improvement
        lr_decay_factor: factor to decay the learning rate with
        min_lr: minimum learning rate
        cb: whether to execute callback function
        track_training: whether to track and print out the training progress
        return_test_score: whether to return the score on the test set (instead of the default validation set)
        detach_core: whether to detach the core from the gradient computation. Used when fine tuning the readout but
                     keeping the core fixed.
        **kwargs:

    Returns:

    """

    if stop_function == "get_loss" and maximize:
        warn("A loss function is the stopping criterion but 'maximize' is set to True for the early stopping")

    def full_objective(model, dataloader, data_key, args, detach_core):
        loss_scale = np.sqrt(len(dataloader[data_key].dataset) / args.images.shape[0]) if scale_loss else 1.0
        regularizers = model.regularizer(data_key=data_key, detach_core=detach_core)
        pupil_center = args.pupil_center if hasattr(args, "pupil_center") else None
        behavior = args.behavior if hasattr(args, "behavior") else None
        output = model(
            args.images.to(device),
            data_key=data_key,
            detach_core=detach_core,
            behavior=behavior,
            pupil_center=pupil_center,
        )
        if hasattr(model, "transform"):
            likelihood = criterion(
                model=model,
                data_key=data_key,
                target=args.responses.to(device),
                output=output,
            )
        else:
            likelihood = criterion(
                target=args.responses.to(device),
                output=output,
            )
        return loss_scale * likelihood + regularizers

    ##### Model training ####################################################################################################
    model.to(device)
    set_random_seed(seed)
    model.train()

    criterion = (
        PoissonLoss(avg=avg_loss) if loss_function == "PoissonLoss" else getattr(losses, loss_function)(avg=avg_loss)
    )
    stop_closure = partial(
        getattr(measures, stop_function),
        dataloaders=dataloaders["validation"],
        device=device,
        per_neuron=False,
        avg=True,
    )
    if stop_function == "get_loss":
        stop_closure = partial(stop_closure, loss_function=loss_function)

    n_iterations = len(LongCycler(dataloaders["train"]))

    optimizer = torch.optim.Adam(model.parameters(), lr=lr_init)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode="max" if maximize else "min",
        factor=lr_decay_factor,
        patience=patience,
        threshold=tolerance,
        min_lr=min_lr,
        verbose=verbose,
        threshold_mode="abs",
    )

    # set the number of iterations over which you would like to accummulate gradients
    optim_step_count = len(dataloaders["train"].keys()) if loss_accum_batch_n is None else loss_accum_batch_n

    if track_training:
        tracker_dict = dict(
            val_correlation=partial(
                measures.get_correlations,
                model,
                dataloaders["validation"],
                device=device,
                per_neuron=False,
            ),
            val_loss=partial(
                measures.get_loss,
                model,
                dataloaders["validation"],
                device=device,
                per_neuron=False,
                avg=False,
                loss_function=loss_function,
            ),
            train_loss=partial(
                measures.get_loss,
                model,
                dataloaders["train"],
                device=device,
                per_neuron=False,
                avg=False,
                loss_function=loss_function,
            ),
        )
        if hasattr(model, "tracked_values"):
            tracker_dict.update(model.tracked_values)
        tracker = MultipleObjectiveTracker(**tracker_dict)
    else:
        tracker = None

    # train over epochs
    for epoch, val_obj in early_stopping(
        model,
        stop_closure,
        interval=interval,
        patience=patience,
        start=epoch,
        max_iter=max_iter,
        maximize=maximize,
        tolerance=tolerance,
        restore_best=restore_best,
        tracker=tracker,
        scheduler=scheduler,
        lr_decay_steps=lr_decay_steps,
    ):

        # print the quantities from tracker
        if verbose and tracker is not None:
            print("=======================================")
            for key in tracker.log.keys():
                print(key, tracker.log[key][-1], flush=True)

        # executes callback function if passed in keyword args
        if cb is not None:
            cb()

        # train over batches
        optimizer.zero_grad()
        for batch_no, (data_key, data) in tqdm(
            enumerate(LongCycler(dataloaders["train"])), total=n_iterations, desc="Epoch {}".format(epoch)
        ):

            loss = full_objective(model, dataloaders["train"], data_key, data, detach_core=detach_core)
            loss.backward()
            if (batch_no + 1) % optim_step_count == 0:
                optimizer.step()
                optimizer.zero_grad()

    ##### Model evaluation ####################################################################################################
    model.eval()
    tracker.finalize() if track_training else None

    # Compute avg validation and test correlation
    validation_correlation = measures.get_correlations(
        model, dataloaders["validation"], device=device, as_dict=False, per_neuron=False
    )
    test_correlation = measures.get_correlations(
        model, dataloaders["test"], device=device, as_dict=False, per_neuron=False
    )

    # return the whole tracker output as a dict
    output = {k: v for k, v in tracker.log.items()} if track_training else {}
    output["validation_corr"] = validation_correlation

    score = np.mean(test_correlation) if return_test_score else np.mean(validation_correlation)
    return score, output, model.state_dict()
