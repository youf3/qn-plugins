import asyncio
import numpy as np
import logging
from enum import Enum

log = logging.getLogger(__name__)


class PSOParams:
    def __init__(self, meas_device):
        self.num_particles = 20
        self.max_iter = 20
        self.threshold_cost1 = 0.02  # 0.015#
        self.threshold_cost2 = 0.05  # 0.025#
        self.threshold_cost_Alice = 0.08  # 0.045#
        self.channels12 = [1, 2]
        self.channels34 = [3, 4]
        self.meas_device = meas_device
        self.visTol = 0.015


class MeasurementType(Enum):
    """Enumeration for different measurement types."""

    COUNT = "count"
    RATE = "rate"
    COINCIDENCE = "coincidence"
    SYNC = "sync"


async def MeasureFunction(meas_device, channels, measurement_time=0.01):
    return await meas_device.measure(MeasurementType.RATE, channels=channels, measurement_time=measurement_time)


async def Measure_CostFunction(node_name, Alice_PSG, meas_device, channels):
    if node_name == "Alice":
        measurement1 = await MeasureFunction(meas_device, channels=[1, 2])
        visibilityH = VisibilityCal(measurement1)
        await Alice_PSG.polarize("D")
        measurement2 = await MeasureFunction(meas_device, channels=[3, 4])
        visibilityD = VisibilityCal(measurement2)
        measurement = measurement1 + measurement2
        cost = visibilityH + visibilityD
        await Alice_PSG.polarize("H")
    elif node_name == "Bob":
        measurement = await MeasureFunction(meas_device, channels)
        cost = VisibilityCal(measurement)
    return measurement, cost


def VisibilityCal(channels):  # TODO Maybe add a way to normalize with respect to Alice/Bob zero power?
    return channels[1] / channels[0]


async def calculate_average_visibility(device, channels, controller=None, voltage=None, num_measurements=10):
    # Set the voltage on the controller
    if controller is not None and voltage is not None:
        await controller.polarize(voltage)

    # Perform measurements and calculate visibility
    total_visibility = 0
    for i in range(num_measurements):
        measure = await MeasureFunction(device, channels)
        visibility = VisibilityCal(measure)
        total_visibility += visibility

    # Calculate the average visibility
    average_visibility = total_visibility / num_measurements

    return average_visibility


async def calculate_user_visibilities(PSG, device, channels, controller=None, voltage=None):
    polarization_states = ["H", "V", "D", "A", "L", "R"]
    visibilities = {}

    for state in polarization_states:
        await PSG.polarize(state)
        avg_visibility = await calculate_average_visibility(device, channels, controller, voltage)
        visibilities[state] = avg_visibility
        log.debug(f"Alice visibility {state}: {avg_visibility}\n")

    return visibilities


async def OSW_operate(
    OSW,
    Alice_Switch_status,
    Bob_Switch_status,
    meas_device,
    channels,
    ch1_params={"channel": "1", "burst_delay": 47e-6, "burst_cycles": 1600, "trigger_source": "EXT"},
    ch2_params={"channel": "2", "burst_delay": 0, "burst_cycles": 1600, "trigger_source": "EXT"},
    initCheck=False,
):
    success = False

    OSW_A, OSW_B = OSW

    while True:
        if Alice_Switch_status == 0:
            await OSW_A.set(**{"channel": 1, "setting": "OFF"})
        elif Alice_Switch_status == 1:
            await OSW_A.configure(**ch1_params)  # TODO: check the current param for ch1
            await OSW_A.set(**{"channel": 1, "setting": "ON"})

        if Bob_Switch_status == 0:
            await OSW_B.set(**{"channel": 2, "setting": "OFF"})
        elif Bob_Switch_status == 1:
            await OSW_B.configure(**ch2_params)  # # TODO: check the current param for ch2
            await OSW_B.set(**{"channel": 2, "setting": "ON"})

        await asyncio.sleep(1)

        power_check = np.sum(await MeasureFunction(meas_device, channels))

        if initCheck:
            break
        else:
            if Alice_Switch_status == 0 and Bob_Switch_status == 0:
                reference_power = meas_device.zero_power
            elif Alice_Switch_status == 0 and Bob_Switch_status == 1:
                reference_power = meas_device.Bob_power
            elif Alice_Switch_status == 1 and Bob_Switch_status == 0:
                reference_power = meas_device.Alice_power
            else:
                reference_power = meas_device.Alice_power + meas_device.Bob_power

            success = True
            break

    return success, power_check
