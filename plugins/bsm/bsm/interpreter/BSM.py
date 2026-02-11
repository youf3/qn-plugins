import sys
import time
import numpy as np
import asyncio
import traceback
import logging
from datetime import datetime, timezone
import json
import signal
from linkCal.interpreter.PSO.PSO import PSO
from linkCal.interpreter.PSO.utility import (
    MeasureFunction,
    PSOParams,
    OSW_operate,
    MeasurementType,
)

log = logging.getLogger(__name__)


def signal_handler(sig, frame):
    print(f"Signal {sig} received. Exiting...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


class PolarizationStabilization:
    def __init__(self, pso_params, duration, tracking_interval=30, visTol=0.015, cb=None):

        self.pso_params = pso_params
        self.duration = duration

        self.tracking_check_interval = tracking_interval
        self.visTol = visTol

        self.SLEEP_TIME_H = 1 * 60
        self.SLEEP_TIME_D = 5 * 60

        self.tracking_data = -np.ones([7, int(2 * duration / tracking_interval)])
        self.cleaned_tracking_data = None

        self.ticPS = time.perf_counter()
        self.tracking_iter = 0

    async def initial_stabilization(self):

        self.PSO = PSO(self.hal, cb=self.__publish)

        # NOTE Step.run() does not check whether self.failed_badly is True or False when self.success = False,
        # only Step.check() does this
        log.info("Running Initial Stabilization...")
        await self.PSO.run_Bob_H1_Stabilization()
        await self.PSO.run_Bob_D2_Stabilization()
        await self.PSO.run_Alice_H1D2_Stabilization()
        await self.PSO.run_Bob_H2_Stabilization()
        log.info("Initial Stabilization Completed")
        await self.only_check(already_checked=True)

    async def only_check(self, already_checked=False):
        # NOTE Only check, no stablization.
        if not already_checked:
            log.info("Checking Step 1")
            await self.PSO.check_Bob_H1_Stabilization()
            await self.PSO.check_Alice_H1H2_Stabilization()
            log.info("Checking Step 2")
            await self.PSO.check_Bob_D2_Stabilization()
            log.info("Checking Step 3")
            await self.PSO.check_Alice_H1D2_Stabilization()
            log.info("Checking Step 4")
            await self.PSO.check_Bob_H2_Stabilization()

        tocPS = time.perf_counter()
        time_elapsed = tocPS - self.ticPS

        log.debug(f"Current time elapsed: {time_elapsed} seconds\n")
        log.debug("Listing visibilities:")
        log.debug(
            f"Time: {time_elapsed:<20}\nBob   H1 {self.PSO.step1_visibility:<20}\n"
            f"Bob   D2 {self.PSO.step2_visibility:<20}\n"
            f"Bob H2 {self.PSO.step4_visibility:<20}\nAlice H1 {self.PSO.H1_visibility:<20}\n"
            f"Alice D2 {self.PSO.D2_visibility:<20}\nAlice H2 {self.PSO.H2_visibility:<20}\n",
        )

        self.tracking_data[:, self.tracking_iter] = np.array(
            [
                time_elapsed,
                self.PSO.step1_visibility,
                self.PSO.step2_visibility,
                self.PSO.step4_visibility,
                self.PSO.H1_visibility,
                self.PSO.D2_visibility,
                self.PSO.H2_visibility,
            ]
        )
        self.tracking_iter += 1
        return

    async def check_H_stability(self):
        log.info("Tracking H polarization stabilization...")
        await self.PSO.check_Bob_H1_Stabilization()
        if not self.PSO.step1_success:
            log.info("Step 1: Failed, rerunning Step 1...")
            await self.PSO.run_Bob_H1_Stabilization()

        await self.PSO.check_Bob_H2_Stabilization()
        if not self.PSO.step4_success:
            log.info("Step 4: Failed, rerunning Step 4...")
            await self.PSO.run_Bob_H2_Stabilization()

        await self.PSO.check_Alice_H1H2_Stabilization()
        if not self.PSO.H1_success or not self.PSO.H2_success:
            log.info("Step 3: Failed, rerunning Step 3...")
            await self.PSO.run_Alice_H1D2_Stabilization()

        log.info("Rechecking H polarization steps...")  # TODO Maybe add self.stepX.check() for all steps?
        if (
            not self.PSO.step1_success
            or not self.PSO.H1_success
            or not self.PSO.H2_success
            or not self.PSO.step4_success
        ):
            log.info("One or more steps failed after checks, rerunning initial stabilization")
            self.initial_stabilization()
        else:
            log.info("All visibilities are below their respective thresholds\nStabilization is good for now!")

    async def check_D_stability(self):
        log.info("Tracking D polarization stabilization...")
        await self.PSO.check_Bob_H1_Stabilization()
        if not self.PSO.step1_success:
            log.info("Step 1: Failed, rerunning Step 1...")
            await self.PSO.run_Bob_H1_Stabilization()

        await self.PSO.check_Bob_D2_Stabilization()
        if not self.PSO.step2_success:
            log.info("Step 2: Failed, rerunning Step 2...")
            await self.PSO.run_Bob_D2_Stabilization()

        await self.PSO.check_Alice_H1D2_Stabilization()
        if not self.PSO.step3_success:
            log.info("Step 3: Failed, rerunning Step 3...")
            await self.PSO.run_Alice_H1D2_Stabilization()

        await self.PSO.check_Bob_H2_Stabilization()
        if not self.PSO.step4_success:
            log.info("Step 4: Failed, rerunning Step 4...")
            await self.PSO.run_Bob_H2_Stabilization()

        log.info("Rechecking D polarization steps...")  # TODO Maybe add self.stepX.check() for all steps?
        if (
            not self.PSO.step1_success
            or not self.PSO.H1_success
            or not self.PSO.D2_success
            or not self.PSO.step4_success
            or not self.PSO.step2_success
        ):
            log.info("One or more steps failed after checks, rerunning initial stabilization")
            await self.initial_stabilization()
        else:
            log.info("All visibilities are below their respective thresholds\nStabilization is good for now!")


class BSM:
    def __init__(self, hal, msgclient, cb=None):
        self.BSM_tracking_data = []
        self.is_pol_init = False
        self.ps = None
        self.cb = cb

        self.msgtopic = "experiment_data"
        self.expid = ""
        self.msgclient = msgclient
        self.hal = hal

        self.Alice_PSG = hal.devs["alice-psg"]
        self.Alice_EPC = hal.devs["alice-epc"]
        self.Charlie_EPC1 = hal.devs["charlie-epc1"]
        self.Charlie_EPC2 = hal.devs["charlie-epc2"]
        self.Charlie_DAQ = hal.devs["charlie-daq"]
        self.Charlie_TimeTagger = hal.devs["charlie-timetagger"]
        self.Charlie_Rigol1 = hal.devs["charlie-rigol1"]
        self.Charlie_Rigol2 = hal.devs["charlie-rigol2"]
        self.Bob_PSG = hal.devs["bob-psg"]

    async def HOM_time_scan(self):
        await self.Alice_PSG.polarize("H")
        await self.Bob_PSG.polarize("H")

        delays = np.arange(000, 10000, 500)
        coincidences_data = []
        # Define channel pairs for coincidences
        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]  # Example: Coincidences between Ch1 and Ch4
        ch1_params = {
            "channel": "1",
            "burst_delay": 0,
            "burst_cycles": 80,
            "burst_period": 10e-6,
            "trigger_source": "INT",
        }
        await self.Charlie_Rigol1.configure(**ch1_params)
        await self.Charlie_Rigol1.set(channel=1, setting="ON")

        ch2_params = {
            "channel": "2",
            "burst_delay": 0,
            "burst_cycles": 80,
            "burst_period": 10e-6,
            "trigger_source": "EXT",
        }
        await self.Charlie_Rigol2.configure(**ch2_params)
        await self.Charlie_Rigol2.set(channel=2, setting="OFF")

        single_counts = await self.Charlie_TimeTagger.measure(
            MeasurementType.RATE, channels=[1, 2, 3, 4], printout=False
        )
        log.debug(f"Alice power: {sum(single_counts)}")

        await self.Charlie_Rigol1.set(channel=1, setting="ON")
        await self.Charlie_Rigol2.set(channel=2, setting="ON")

        await asyncio.sleep(1)
        single_counts = await self.Charlie_TimeTagger.measure(
            MeasurementType.RATE, channels=[1, 2, 3, 4], printout=False
        )
        log.debug(f"Bob + Alice power: {sum(single_counts)}")

        await self.Charlie_Rigol1.set(channel=1, setting="ON")
        await self.Charlie_Rigol2.set(channel=2, setting="ON")

        for i, delay in enumerate(delays):
            burst_delay = delay * 1e-9
            await self.Charlie_Rigol2.configure(channel=2, raw_command=f"SOUR2:BURS:TDEL {burst_delay}")

            # Get coincidences for Ch1 and Ch4
            coincidence_hist = await self.Charlie_TimeTagger.measure(
                MeasurementType.COINCIDENCE, channels=channel_pairs, measurement_time=1, binwidth=0.1e6, n_bins=100
            )  # binwidth in ps (check this)
            HOM_coincidence_hist = coincidence_hist[1]
            hist_str = np.array2string(HOM_coincidence_hist, precision=2, separator=", ", suppress_small=True)
            log.debug(f"Delay: {delay*1E-3:.3f}us, Coin counts: {np.sum(HOM_coincidence_hist):.0f}")
            log.debug(f"Histogram: {hist_str}")

            coincidences_data.append(np.sum(HOM_coincidence_hist))
            HOM_vis = (np.mean(np.sort(coincidences_data)[-5:]) - min(coincidences_data)) / np.mean(
                np.sort(coincidences_data)[-5:]
            )
            await asyncio.sleep(0.01)
        log.debug(f"HOM visibility:{HOM_vis}")
        await self.__publish({"HOM_visibility": HOM_vis, "HOM_coincidences": coincidences_data})

        log.debug(np.argmin(coincidences_data))
        log.debug(f"Best delay: {delays[np.argmin(coincidences_data)]*1E-3} us ")
        best_delay_ns = delays[np.argmin(coincidences_data)] * 1e-9
        await self.Charlie_Rigol2.configure(channel=2, raw_command=f"SOUR2:BURS:TDEL {best_delay_ns}")

        return {
            "visibility": HOM_vis,
            "total_coincidences": np.sum(coincidences_data),
            "best_delay_ns": best_delay_ns,
            "coincidence_data": coincidences_data,
        }

    async def BSM_psi_plus(self, measurement_time=1):

        await self.Alice_PSG.polarize("D")
        await self.Bob_PSG.polarize("D")

        await self.Charlie_Rigol1.set(channel=1, setting="ON")
        await self.Charlie_Rigol2.set(channel=2, setting="ON")

        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
        coincidence_hist = await self.Charlie_TimeTagger.measure(
            MeasurementType.COINCIDENCE,
            channels=channel_pairs,
            binwidth=0.1e6,
            n_bins=100,
            measurement_time=measurement_time,
        )  # binwidth in ps (check this)

        countrate = await self.Charlie_TimeTagger.measure(MeasurementType.RATE, channels=[1, 2, 3, 4])
        for j in range(len(coincidence_hist)):
            log.debug(f"Channel pair: {channel_pairs[j]}, Total coincidences: {np.sum(coincidence_hist[j])}")
            log.debug(countrate)
        coincidence_his_sum = np.sum(coincidence_hist, axis=1)
        log.debug(coincidence_his_sum)
        await self.__publish({"coincidence_his_sum_psi+": coincidence_his_sum.tolist()})
        psi_p = np.sum(coincidence_hist[0]) + np.sum(coincidence_hist[5])
        psi_m = np.sum(coincidence_hist[2]) + np.sum(coincidence_hist[3])

        vis = (psi_p - psi_m) / psi_p
        log.debug(f"Visibility:{vis}")

        return psi_p, psi_m

    async def BSM_psi_plus_fast(self, measurement_time=1):

        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
        coincidence_hist = await self.Charlie_TimeTagger.measure(
            MeasurementType.COINCIDENCE,
            channels=channel_pairs,
            binwidth=0.1e6,
            n_bins=100,
            measurement_time=measurement_time,
        )  # binwidth in ps (check this)

        psi_p = np.sum(coincidence_hist[0]) + np.sum(coincidence_hist[5])
        psi_m = np.sum(coincidence_hist[2]) + np.sum(coincidence_hist[3])

        vis = (psi_p - psi_m) / psi_p
        log.debug(f"Visibility:{vis}")

        return psi_p, psi_m

    async def BSM_psi_minus(self, measurement_time=1):

        await self.Alice_PSG.polarize("D")
        await self.Bob_PSG.polarize("A")

        await self.Charlie_Rigol1.set(channel=1, setting="ON")
        await self.Charlie_Rigol2.set(channel=2, setting="ON")

        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
        coincidence_hist = await self.Charlie_TimeTagger.measure(
            MeasurementType.COINCIDENCE,
            channels=channel_pairs,
            binwidth=0.1e6,
            n_bins=100,
            measurement_time=measurement_time,
        )  # binwidth in ps (check this)

        for j in range(len(coincidence_hist)):
            log.debug(f"Channel pair: {channel_pairs[j]}, Total coincidences: {np.sum(coincidence_hist[j])}")
        coincidence_his_sum = np.sum(coincidence_hist, axis=1)
        await self.__publish({"coincidence_his_sum_psi-": coincidence_his_sum.tolist()})
        log.debug(coincidence_his_sum)

        psi_p = np.sum(coincidence_hist[0]) + np.sum(coincidence_hist[5])
        psi_m = np.sum(coincidence_hist[2]) + np.sum(coincidence_hist[3])

        vis = (psi_m - psi_p) / psi_m
        log.debug(f"Visibility:{vis}")
        return psi_p, psi_m

    async def BSM_psi_minus_fast(self, measurement_time=1):

        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
        coincidence_hist = await self.Charlie_TimeTagger.measure(
            MeasurementType.COINCIDENCE,
            channels=channel_pairs,
            binwidth=0.1e6,
            n_bins=100,
            measurement_time=measurement_time,
        )  # binwidth in ps (check this)

        psi_p = np.sum(coincidence_hist[0]) + np.sum(coincidence_hist[5])
        psi_m = np.sum(coincidence_hist[2]) + np.sum(coincidence_hist[3])

        vis = (psi_m - psi_p) / psi_m
        log.debug(f"Visibility:{vis}")

        return psi_p, psi_m

    async def init_device(self):
        # TODO Add conditions checking Bob visibilities during steps 1 and 2 and Alice checking that Bob's visibilities
        # are reasonable

        self.num_runs = 1  # it can take around 10-25 minutes to run
        duration = 2 * 60 * 60  # amount of time to run the stabilization algorithm for
        tracking_interval = 30
        # Define Alice and Bob visibilities dictionaries to save to .pkl files later

        message = "Particle Swarm Optimization Polarization Stabilization Test"
        "step-by-step log"  # message should not be larger than 116 characters

        notes = "(Clipping at the boundaries and resetting a particle's velocity and voltage to a random value"
        notes2 = "if it hits the boundary)"
        notes3 = "Using LBNL laser and time tagger with live-plotting"
        log.debug("[" + "=" * 118 + "]")
        log.debug(message)
        log.debug(notes)
        log.debug(notes2)
        log.debug(notes3)
        log.debug("[" + "=" * 118 + "]\n")

        # Initialize devices
        log.debug("##### Initializing Alice's devices #####")

        await self.Alice_PSG.connect()
        await self.Alice_EPC.connect()
        log.debug("##### Alice's devices are initialized #####")

        log.debug("##### Initializing Bob's devices ##### \n")

        await self.Bob_PSG.connect()

        log.debug("##### Bob's devices are initialized #####\n")

        log.debug("##### Initializing Charlie's devices #####\n")

        await self.Charlie_Rigol1.connect()
        ch1_params = {"channel": 1, "burst_delay": 5.4e-6, "burst_cycles": 780, "trigger_source": "INT"}
        await self.Charlie_Rigol1.configure(**ch1_params)
        await self.Charlie_Rigol2.connect()
        ch2_params = {"channel": 2, "burst_delay": 0, "burst_cycles": 780, "trigger_source": "EXT"}
        await self.Charlie_Rigol2.configure(**ch2_params)

        # COM 8 V_2π = 0.887 V, COM 11 V_2π = 0.94 V, COM 12 V_2π = 0.752
        await self.Charlie_EPC1.connect()
        await self.Charlie_EPC2.connect()
        await self.Charlie_TimeTagger.connect()

        self.Charlie_meas_device = self.Charlie_TimeTagger
        log.debug("##### Charlie's devices are initialized #####\n")

        pso_params = PSOParams(self.Charlie_meas_device)

        # Default OSW connetion
        await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.Charlie_meas_device,
            channels=[1, 2, 3, 4],
            ch1_params=ch1_params,
            ch2_params=ch2_params,
            initCheck=True,
        )
        raw_Alice_power = await MeasureFunction(self.Charlie_meas_device, [1, 2, 3, 4])
        log.debug(f"Alice raw power: {raw_Alice_power}")
        self.Charlie_meas_device.Alice_power = np.sum(raw_Alice_power)
        log.debug(f"Alice power: {self.Charlie_meas_device.Alice_power}")

        _, Bob_power = await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_meas_device,
            channels=[1, 2, 3, 4],
            ch1_params=ch1_params,
            ch2_params=ch2_params,
            initCheck=True,
        )
        raw_Bob_power = await MeasureFunction(self.Charlie_meas_device, [1, 2, 3, 4])
        log.debug(f"Bob power after switch: {Bob_power}")
        log.debug(f"Bob raw power: {raw_Bob_power}")
        self.Charlie_meas_device.Bob_power = np.sum(raw_Bob_power)
        log.debug(f"Bob power: {self.Charlie_meas_device.Bob_power}")

        await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=0,
            meas_device=self.Charlie_meas_device,
            channels=[1, 2, 3, 4],
            ch1_params=ch1_params,
            ch2_params=ch2_params,
            initCheck=True,
        )
        raw_zero_power = await MeasureFunction(self.Charlie_meas_device, [1, 2, 3, 4])
        log.debug(f"Raw zero power: {raw_zero_power}")
        self.Charlie_meas_device.zero_power = raw_zero_power
        log.debug(f"measure zero power: {self.Charlie_meas_device.zero_power}")

        try:
            self.tic_total = time.perf_counter()
            self.run_plural = "s" if self.num_runs != 1 else ""

            self.ps = PolarizationStabilization(pso_params, duration, tracking_interval, cb=self.__publish)
            # ps.initial_stabilization()

            min_delay = 45000
            ch1_params = {
                "channel": 1,
                "amplitude": 1,
                "burst_delay": min_delay * 1e-9,
                "burst_cycles": 800,
                "trigger_source": "INT",
            }
            await self.Charlie_Rigol1.configure(**ch1_params)
            await self.Charlie_Rigol1.set(channel=1, setting="ON")

            ch2_params = {"channel": 2, "amplitude": 1, "burst_delay": 0, "burst_cycles": 800, "trigger_source": "EXT"}

            await self.Charlie_Rigol2.configure(**ch2_params)
            await self.Charlie_Rigol2.set(channel=2, setting="ON")
            return 0

        except KeyboardInterrupt:
            log.info("Polarization stabilization interrupted by user.")
            return 1
        except Exception as e:
            log.info(f"An error occurred: {e}")
            traceback.print_exc()  # Print full traceback
            return 1

    async def stabilization(self):
        try:
            if not self.is_pol_init:
                self.ps.initial_stabilization()
                self.ps.clean_tracking_data()
                await self.__publish({"pol_tracking": [list(i) for i in self.ps.cleaned_tracking_data.T]})
                self.is_pol_init = True
            else:

                self.ps.only_check()
                self.ps.clean_tracking_data()
                await self.__publish({"pol_tracking": [list(i) for i in self.ps.cleaned_tracking_data.T]})

                self.ps.check_D_stability()
                self.ps.only_check()
                self.ps.clean_tracking_data()
                await self.__publish({"pol_tracking": [list(i) for i in self.ps.cleaned_tracking_data.T]})

            return 0

        except KeyboardInterrupt:
            log.info("Polarization stabilization interrupted by user.")
            return 1
        except Exception as e:
            log.info(f"An error occurred: {e}")
            traceback.print_exc()  # Print full traceback
            return 1

    async def bsm(self):
        await asyncio.sleep(3)
        await self.HOM_time_scan()
        await self.BSM_psi_plus()
        # for i in range(60):
        #     psi_p, psi_m = await self.BSM_psi_plus_fast()
        #     BSM_vis = (psi_p - psi_m) / psi_p
        #     tocPS = time.perf_counter()
        #     time_elapsed = tocPS - self.ps.ticPS
        #     self.BSM_tracking_data.append([time_elapsed, psi_p, psi_m])
        #     await self.__publish({"bsm_tracking": [[time_elapsed, int(psi_p), int(psi_m), int(BSM_vis * 100)]]})

        #     # Add periodic yielding to prevent blocking
        #     if i % 10 == 0:
        #         await asyncio.sleep(0)  # Yield control to event loop

        # self.ps.check_H_stability()
        # self.ps.only_check()

        await self.BSM_psi_minus()
        # for i in range(60):
        #     psi_p, psi_m = await self.BSM_psi_minus_fast()
        #     BSM_vis = (psi_m - psi_p) / psi_m
        #     tocPS = time.perf_counter()
        #     time_elapsed = tocPS - self.ps.ticPS
        #     self.BSM_tracking_data.append([time_elapsed, psi_p, psi_m])
        #     await self.__publish({"bsm_tracking": [[time_elapsed, int(psi_p), int(psi_m), int(BSM_vis * 100)]]})

        #     # Add periodic yielding to prevent blocking
        #     if i % 10 == 0:
        #         await asyncio.sleep(0)  # Yield control to event loop

        # self.ps.check_D_stability()
        # self.ps.only_check()
        return 0

    async def cleanup(self):
        toc_total = time.perf_counter()
        delta_t_total = toc_total - self.tic_total
        delta_t_total_hr = int(delta_t_total / 3600)
        delta_t_total_min = int((delta_t_total % 3600) / 60)
        delta_t_total_sec = delta_t_total % 60
        log.info(
            f"\nTotal time elapsed for all {self.num_runs} Run{self.run_plural} is {delta_t_total_hr} hours,"
            f"{delta_t_total_min} minutes and {delta_t_total_sec} seconds",
        )

        # log.info( "end")
        # Alice.laser.turn_off()
        # self.Alice.disconnect_all()
        # log.debug( "Alice's laser is off")
        # Bob.laser.turn_off()
        # self.Bob.disconnect_all()
        # log.debug("Bob's laser is off")
        await self.Charlie_Rigol1.set(channel=1, setting="OFF")
        await self.Charlie_Rigol2.control_channel(channel=2, setting="OFF")
        # self.Charlie.disconnect_all()
        # self.Charlie.OSW.disconnect()

    async def __publish(self, payload):
        if self.cb:
            await self.cb(payload)
        else:
            msg = json.dumps({"expid": self.expid, "ts": datetime.now(timezone.utc).timestamp(), "data": payload})
            await self.hal._msgclient.publish(self.msgtopic, msg)
