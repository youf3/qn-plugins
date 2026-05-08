import numpy as np
import time
import logging
from .utility import (
    MeasureFunction,
    Measure_CostFunction,
    PSOParams,
    OSW_operate,
    calculate_average_visibility,
    calculate_user_visibilities,
)

log = logging.getLogger(__name__)


class Particle:
    def __init__(self):
        self.dim = 3
        self.w = 1
        self.c1 = 2
        self.c2 = 2
        self.bounds = np.array([(0.1, 1.1)] * self.dim)
        self.voltage = np.random.uniform(self.bounds[:, 0], self.bounds[:, 1], self.dim)
        self.velocity = np.random.uniform(-1, 1, self.dim)
        self.best_voltage = self.voltage.copy()
        self.best_cost = float("inf")
        self.cost = float("inf")

    def update_velocity(self, global_best_voltage, relTol=0.01):
        r1, r2 = np.random.rand(), np.random.rand()
        delta_V_cognitive = self.best_voltage - self.voltage
        delta_V_social = global_best_voltage - self.voltage
        cognitive = self.c1 * r1 * delta_V_cognitive
        social = self.c2 * r2 * delta_V_social
        apply_velocity = np.int32((delta_V_cognitive > relTol) | (delta_V_social > relTol))
        self.velocity = self.w * self.velocity * apply_velocity + cognitive + social

    def update_voltage(self):
        temp_voltage = self.voltage + self.velocity
        outOfBounds = (temp_voltage > self.bounds[:, 1]) | (temp_voltage < self.bounds[:, 0])
        self.velocity = np.where(outOfBounds, np.random.uniform(-1, 1, self.dim), self.velocity)
        self.voltage = np.where(
            outOfBounds, np.random.uniform(self.bounds[0, 0], self.bounds[0, 1], self.dim), self.voltage + self.velocity
        )
        self.voltage = np.clip(self.voltage, self.bounds[:, 0], self.bounds[:, 1])


class PSOManager:
    def __init__(self, pso_params, threshold_cost):
        if isinstance(pso_params, dict):
            self.num_particles = pso_params["num_particles"]
            self.max_iter = pso_params["max_iter"]
            self.meas_device = pso_params["meas_device"]
        else:  # Assuming pso_params is PSOParams
            self.num_particles = pso_params.num_particles
            self.max_iter = pso_params.max_iter
            self.meas_device = pso_params.meas_device

        self.threshold_cost = threshold_cost

        self.init_particles()

    def init_particles(self):
        self.particle = Particle()
        self.w = self.particle.w
        self.particles = [Particle() for _ in range(self.num_particles)]
        self.global_best_voltage = np.random.uniform(
            self.particle.bounds[:, 0], self.particle.bounds[:, 1], self.particle.dim
        )
        self.global_best_cost = float("inf")

    async def evaluate_cost(self, node_name, PSG, user_ctrl, channels, voltage, reference_cost):
        max_tries = 5
        temp_cost = np.zeros(max_tries)
        await user_ctrl.polarize(voltage)
        for i in range(max_tries):
            _, temp_cost[i] = await Measure_CostFunction(node_name, PSG, self.meas_device, channels)
        mean_cost = np.mean(temp_cost)
        return mean_cost, mean_cost <= reference_cost

    async def optimize(self, node_name, PSG, user_ctrl, channels):
        log.info("\nOptimization Summary")
        log.info("=" * 120)
        log.info(
            f"{'Iteration':<12}{'Particle No.':<15}{'Voltage':<25}{'Measurement':<20}{'Cost':<15}{'Best Cost':<15}"
            f"{'Global Best Cost':<20}",
        )
        log.info("-" * 120)

        for iteration in range(self.max_iter):
            tic = time.perf_counter()
            for particle_no, particle in enumerate(self.particles):
                await user_ctrl.polarize(particle.voltage)
                particle.measurement, particle.cost = await Measure_CostFunction(
                    node_name, PSG, self.meas_device, channels
                )

                if particle.cost < particle.best_cost:
                    particle.cost, success = await self.evaluate_cost(
                        node_name, PSG, user_ctrl, channels, particle.voltage, particle.best_cost
                    )
                    if success:
                        particle.best_cost = particle.cost
                        particle.best_voltage = particle.voltage.copy()

                if particle.cost < self.global_best_cost:
                    particle.cost, success = await self.evaluate_cost(
                        node_name, PSG, user_ctrl, channels, particle.voltage, self.global_best_cost
                    )
                    if success:
                        self.global_best_cost = particle.cost
                        self.global_best_voltage = particle.voltage.copy()
                log.info(
                    f"{iteration + 1:<12}{particle_no:<15}{str(particle.voltage):<25}"
                    f"{str(particle.measurement.ravel()):<20}{particle.cost:<15.5f}"
                    f"{particle.best_cost:<15.5f}{self.global_best_cost:<20.5f}",
                )

            if np.round(self.global_best_cost, 4) <= self.threshold_cost:
                self.global_best_cost, success = await self.evaluate_cost(
                    node_name, PSG, user_ctrl, channels, self.global_best_voltage, self.threshold_cost
                )
                if success:
                    log.debug(f"Threshold cost achieved at iteration {iteration + 1}. Optimization stopped.")
                    break

            toc = time.perf_counter()

            log.debug(
                f"Iteration {iteration + 1}/{self.max_iter} | Best voltage: {self.global_best_voltage} | "
                f"Best Cost: {self.global_best_cost}, time elapsed {toc - tic} seconds\n",
            )

            for particle in self.particles:
                particle.update_velocity(self.global_best_voltage)
                particle.update_voltage()
            self.w *= 0.99

        return self.global_best_voltage, self.global_best_cost

    async def optimize_polarization(self, node_name, PSG, pol, channels, user_ctrl):
        success = False
        self.init_particles()
        for _ in range(2):
            _ = await PSG.polarize(pol)
            best_voltage, best_cost = await self.optimize(node_name, PSG, user_ctrl, channels)
            _, user_pol_visibility = await Measure_CostFunction(
                node_name, PSG, meas_device=self.meas_device, channels=channels
            )
            log.info(f"user pol visibility: {user_pol_visibility}")
            if np.round(user_pol_visibility, 3) <= self.threshold_cost:
                success = True
                break
        log.info(f"Optimal Voltage for polarization control: {best_voltage}")
        log.info(f"Minimum Visibility: {best_cost}")
        return best_voltage, best_cost, success


class PSO:
    def __init__(self, hal, cb=None):
        self.hal = hal
        # Devices
        self.Alice_PSG = hal.devs["alice-psg"]
        self.Alice_EPC = hal.devs["alice-epc"]
        self.Charlie_EPC1 = hal.devs["charlie-epc1"]
        self.Charlie_EPC2 = hal.devs["charlie-epc2"]
        self.Charlie_DAQ = hal.devs["charlie-daq"]
        self.Charlie_TimeTagger = hal.devs["charlie-timetagger"]
        self.Charlie_Rigol1 = hal.devs["charlie-rigol1"]
        self.Charlie_Rigol2 = hal.devs["charlie-rigol2"]
        self.Bob_PSG = hal.devs["bob-psg"]
        self.MAX_TRIES = 5
        self.ch1_params = {
            "channel": "1",
            "burst": False,
            "burst_delay": 47e-6,
            "burst_cycles": 1600,
            "trigger_source": "INT",
        }
        self.ch2_params = {
            "channel": "2",
            "burst": False,
            "burst_delay": 0,
            "burst_cycles": 1600,
            "trigger_source": "INT",
        }

        # Parameters
        self.num_runs = 1
        self.duration = 2 * 60 * 60  # 2 hours
        self.tracking_interval = 30

        # Data containers
        self.Alice_visibilities_dict = {}
        self.Bob_visibilities_dict = {}
        self.timing = np.zeros((self.num_runs))

        # PSO parameters placeholder (to be set in async init)
        self.dim = 3
        self.pso_params = None
        self.cb = cb

        self.step1_success = False
        self.step1_failed_badly = True
        self.step2_success = False
        self.step2_failed_badly = True
        self.step3_success = False
        self.step3_failed_badly = True
        self.step3_best_voltage = None
        self.step4_success = False
        self.step4_failed_badly = True

        self.H1_success = False
        self.D2_success = False
        self.H2_success = False

    async def init(self):
        log.info("##### Initializing Alice's devices #####")
        await self.Alice_PSG.connect()
        await self.Alice_EPC.connect()
        log.info("##### Alice's devices are initialized #####")
        log.info("##### Initializing Bob's devices ##### \n")
        # Bob's devices initialization is async, handled in async init
        log.info("##### Initializing Charlie's devices #####\n")
        for Rigol in [self.Charlie_Rigol1, self.Charlie_Rigol2]:
            await Rigol.connect()
            await Rigol.configure(**self.ch1_params)
            await Rigol.configure(**self.ch2_params)
        await self.Charlie_EPC1.connect()
        await self.Charlie_EPC2.connect()
        await self.Charlie_TimeTagger.connect()  # Connect Charlie's TimeTagger
        log.info("##### Charlie's devices are initialized #####\n")

        # Await async calls such as Bob's device initialization via RPC
        await self.Bob_PSG.connect()
        log.info("##### Bob's devices are initialized #####\n")

        # Initialize PSO parameters after async device init
        self.pso_params = PSOParams(self.Charlie_TimeTagger)

        # Additional setup (e.g., OSW operation, power measurements)
        await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
            initCheck=True,
        )
        raw_Alice_power = await MeasureFunction(self.Charlie_TimeTagger, [1, 2, 3, 4])
        log.debug(f"Alice raw power: {raw_Alice_power}")
        self.Charlie_TimeTagger.Alice_power = np.sum(raw_Alice_power)
        log.debug(f"Alice power: {self.Charlie_TimeTagger.Alice_power}")

        _, Bob_power = await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
            initCheck=True,
        )
        raw_Bob_power = await MeasureFunction(self.Charlie_TimeTagger, [1, 2, 3, 4])
        log.debug(f"Bob power after switch: {Bob_power}")
        log.debug(f"Bob raw power: {raw_Bob_power}")
        self.Charlie_TimeTagger.Bob_power = np.sum(raw_Bob_power)
        log.debug(f"Bob power: {self.Charlie_TimeTagger.Bob_power}")

        # Measure zero power with OSW off
        await OSW_operate(
            OSW=[self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=0,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
            initCheck=True,
        )
        raw_zero_power = await MeasureFunction(self.Charlie_TimeTagger, [1, 2, 3, 4])
        log.debug(f"Raw zero power: {raw_zero_power}")
        self.Charlie_TimeTagger.zero_power = raw_zero_power
        log.debug(f"measure zero power: {self.Charlie_TimeTagger.zero_power}")

        # You can add a condition to break or continue the loop here if needed

    async def run_Bob_H1_Stabilization(self):
        self.MAX_TRIES = 1  # TODO: change this to 5
        ticBH = time.perf_counter()
        pso = PSOManager(self.pso_params, self.pso_params.threshold_cost1)

        log.info("Running Bob_H1_Stabilization...")
        log.info("Setting H polarization from Bob's PSG")

        await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],  # TODO Need to check channels for TimeTaggerManager (is 6 given as 2 or 6?)
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        num_tries = 0
        while num_tries <= self.MAX_TRIES and not self.step1_success:
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            self.step1_best_voltage, self.step1_visibility, self.step1_success = await pso.optimize_polarization(
                node_name="Bob",
                PSG=self.Bob_PSG,
                pol="H",
                channels=self.pso_params.channels12,
                user_ctrl=self.Charlie_EPC1,
            )

            if self.step1_success:
                self.step1_failed_badly = False

            num_tries += 1
            break  # TODO: remove this line

        tocBH = time.perf_counter()
        log.info(f"Time taken for Bob H1 Stabilization: {tocBH - ticBH} seconds")
        log.info("Bob_H1_Stabilization Completed Successfully")

    async def check_Bob_H1_Stabilization(self):
        """Checks the current visibility or polarization stability for Bob H1"""
        log.debug("Setting H polarization from Bob's PSG")
        await self.Bob_PSG.polarize("H")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        Bob_H1_avg_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels12,
            controller=self.Charlie_EPC1,
            voltage=self.step1_best_voltage,
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        log.debug(f"Bob H1 visibility: {Bob_H1_avg_visibility}")
        if self.cb is not None:
            await self.cb({"pol_tracking": {"Bob_H1": Bob_H1_avg_visibility}})

        if Bob_H1_avg_visibility <= self.pso_params.threshold_cost1:
            self.step1_success = True
        else:
            self.step1_success = False
            if Bob_H1_avg_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost1:
                self.step1_failed_badly = False
            else:
                self.step1_failed_badly = True
        log.info(f"Bob_H1_Stabilization visibility: {'Valid' if self.step1_success else 'Failed'}")

        self.step1_visibility = Bob_H1_avg_visibility

    async def run_Bob_D2_Stabilization(self):
        ticBD = time.perf_counter()
        pso = PSOManager(self.pso_params, self.pso_params.threshold_cost2)

        log.info("Running Bob_D2_Stabilization...")
        log.debug("Setting D polarization from Bob's PSG")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        # self.check()
        num_tries = 0
        while num_tries <= self.MAX_TRIES and not self.step2_success:
            # Optimize Charlie Pol_CTRL2 to find best PSO voltage and cost
            self.step2_best_voltage, self.step2_visibility, self.step2_success = await pso.optimize_polarization(
                node_name="Bob",
                PSG=self.Bob_PSG,
                pol="D",
                channels=self.pso_params.channels34,
                user_ctrl=self.Charlie_EPC2,
            )

            if self.step2_success:
                self.step2_failed_badly = False

            num_tries += 1

        tocBD = time.perf_counter()
        log.info(f"Time taken for Bob D2 Stabilization: {tocBD - ticBD} seconds")

        log.info("Bob_D2_Stabilization Completed Successfully")

    async def check_Bob_D2_Stabilization(self):
        """Checks the current visibility or polarization stability for Bob D2"""
        log.debug("Setting D polarization from Bob's PSG")
        await self.Bob_PSG.polarize("D")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        Bob_D2_avg_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels34,
            controller=self.Charlie_EPC2,
            voltage=self.step2_best_voltage,
        )

        log.debug(f"Bob D2 visibility: {Bob_D2_avg_visibility}")

        if Bob_D2_avg_visibility <= self.pso_params.threshold_cost2:
            self.step2_success = True
        else:
            self.step2_success = False
            if Bob_D2_avg_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost2:
                self.step2_failed_badly = False
            else:
                self.step2_failed_badly = True
        log.info(f"Bob_D2_Stabilization visibility: {'Valid' if self.step2_success else 'Failed'}")

        self.step2_visibility = Bob_D2_avg_visibility
        if self.cb is not None:
            await self.cb({"pol_tracking": {"Bob_D2": Bob_D2_avg_visibility}})

    async def run_Alice_H1D2_Stabilization(self):
        MAX_TRIES = 10
        num_tries = 0
        ticAH = time.perf_counter()
        pso = PSOManager(self.pso_params, self.pso_params.threshold_cost_Alice)

        log.info("Running Alice_H1D2_Stabilization...")
        log.debug("Setting H polarization from Alice's PSG")

        # log.info(f"Running {self.name}... Checking Step 1 and Step 2 validity")

        all_succeeded = self.step3_success and self.step1_success and self.step2_success
        while num_tries < MAX_TRIES and not all_succeeded:
            num_tries += 1
            _ = await OSW_operate(
                [self.Charlie_Rigol1, self.Charlie_Rigol2],
                Alice_Switch_status=1,
                Bob_Switch_status=0,
                meas_device=self.Charlie_TimeTagger,
                channels=[1, 2, 3, 4],
                ch1_params=self.ch1_params,
                ch2_params=self.ch2_params,
            )
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            if self.step1_success and self.step2_success and (not self.H1_success or not self.D2_success):
                self.step3_best_voltage, self.best_cost, self.step3_success = await pso.optimize_polarization(
                    node_name="Alice",
                    PSG=self.Alice_PSG,
                    pol="H",
                    channels=self.pso_params.channels12,
                    user_ctrl=self.Alice_EPC,
                )

            # Revalidate Step 1 and Step 2
            await self.check_Bob_H1_Stabilization()
            await self.check_Bob_D2_Stabilization()

            # Retry only failed steps
            if not self.step1_success:
                log.info("Step 1 visibilites are above threshold, Redoing Step 1")
                await self.run_Bob_H1_Stabilization()
            if not self.step2_success:
                log.info("Step 2 visibilites are above threshold, Redoing Step 2")
                await self.run_Bob_D2_Stabilization()

            await self.check_Alice_H1D2_Stabilization()

            log.debug(f"Alice H1 visibility: {self.H1_visibility}, Alice D2 visibility: {self.D2_visibility}")

            for key in self.Alice_all_visibilities.keys():
                log.info(f"Alice {key} visibility: {self.Alice_all_visibilities[key]}")

            # if step1_valid and step2_valid:
            #     break # If both are valid, Step 3 is successful

            all_succeeded = (
                self.step3_success and self.step1_success and self.step2_success
            )  # TODO fix this to only deal with Alice

        if num_tries == MAX_TRIES:
            return  #

        if self.step3_success:
            self.step3_failed_badly = False

        log.info("Alice_H1H2_Stabilization Completed Successfully")
        tocAH = time.perf_counter()
        log.info(f"Time taken for Alice H1 and D2 Stabilization: {tocAH - ticAH} seconds")

    async def check_Alice_H1D2_Stabilization(self):
        """Checks the current visibility or polarization stability for Alice H1 and D2"""
        log.debug("Checking Alice's current visibilities...")
        log.debug("Setting H polarization from Alice's PSG")
        await self.Alice_PSG.polarize("H")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        self.H1_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels12,
            # It is actually important to add these last two arguments since we are constantly checking
            # if each step is still vaild
            controller=self.Alice_EPC,
            voltage=self.step3_best_voltage,
        )

        log.debug(f"Setting D polarization from Alice's PSG: best_voltage = {self.step3_best_voltage}")
        await self.Alice_PSG.polarize("D")  # TODO Need to be able to check Alice H2 as well

        self.D2_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels34,
            controller=self.Alice_EPC,
            voltage=self.step3_best_voltage,
        )

        self.Alice_all_visibilities = await calculate_user_visibilities(
            PSG=self.Alice_PSG,
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels12,
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        log.debug(f"Alice H1 visibility: {self.H1_visibility}, Alice D2 visibility: {self.D2_visibility}")

        if self.H1_visibility <= self.pso_params.threshold_cost_Alice:
            self.H1_success = True
            self.H1_failed_badly = False
        else:
            self.H1_success = False
            if self.H1_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost_Alice:
                self.H1_failed_badly = False
            else:
                self.H1_failed_badly = True

        if self.D2_visibility <= self.pso_params.threshold_cost_Alice:
            self.D2_success = True
            self.D2_failed_badly = False
        else:
            self.D2_success = False
            if self.D2_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost_Alice:
                self.D2_failed_badly = False
            else:
                self.D2_failed_badly = True

        self.step3_success = self.H1_success and self.D2_success
        self.step3_failed_badly = self.H1_failed_badly or self.D2_failed_badly
        log.info(f"Alice_H1D2_Stabilization visibility: {'Valid' if self.step3_success else 'Failed'}")

        if self.cb is not None:
            await self.cb({"pol_tracking": {"Alice_H1": self.H1_visibility, "Alice_D2": self.D2_visibility}})

    async def check_Alice_H1H2_Stabilization(self):
        """Checks the current visibility or polarization stability for Alice H1 and H2"""
        log.info("Checking Alice's current visibilities...")
        log.debug("Setting H polarization from Alice's PSG")
        await self.Alice_PSG.polarize("H")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )
        self.H1_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels12,
            # It is actually important to add these last two arguments since we are constantly checking
            # if each step is still vaild
            controller=self.Alice_EPC,
            voltage=self.step3_best_voltage,
        )

        # log.debug("Setting D polarization from Alice's PSG")
        # self.Alice_PSG.polSET(self.Alice_PSG.D) # TODO Need to be able to check Alice H2 as well

        self.H2_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels34,
            controller=self.Alice_EPC,
            voltage=self.step3_best_voltage,
        )

        self.Alice_all_visibilities = await calculate_user_visibilities(
            PSG=self.Alice_PSG,
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels12,
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        log.debug(f"Alice H1 visibility: {self.H1_visibility}, Alice H2 visibility: {self.H2_visibility}")

        if self.H1_visibility <= self.pso_params.threshold_cost_Alice:
            self.H1_success = True
            self.H1_failed_badly = False
        else:
            self.H1_success = False
            if self.H1_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost_Alice:
                self.H1_failed_badly = False
            else:
                self.H1_failed_badly = True

        if self.H2_visibility <= self.pso_params.threshold_cost_Alice:
            self.H2_success = True
            self.H2_failed_badly = False
        else:
            self.H2_success = False
            if self.H2_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost_Alice:
                self.H2_failed_badly = False
            else:
                self.H2_failed_badly = True

        self.step3_success = self.H1_success and self.H2_success
        self.step3_failed_badly = self.H1_failed_badly or self.H2_failed_badly
        log.info(f"Alice_H1H2_Stabilization visibility: {'Valid' if self.step3_success else 'Failed'}")

        if self.cb is not None:
            await self.cb({"pol_tracking": {"Alice_H1": self.H1_visibility, "Alice_H2": self.H2_visibility}})

    async def run_Bob_H2_Stabilization(self):
        # self.step4_success = False
        # self.step4_failed_badly = True
        ticBH = time.perf_counter()
        pso = PSOManager(self.pso_params, self.pso_params.threshold_cost2)

        log.info("Running Bob_H2_Stabilization...")
        log.debug("Setting H polarization from Bob's PSG")
        # self.Bob.PSG.polSET(self.Bob.PSG.H)

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        # self.check()
        num_tries = 0
        while num_tries <= self.MAX_TRIES and not self.step4_success:
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            self.step4_best_voltage, self.step4_visibility, self.step4_success = await pso.optimize_polarization(
                node_name="Bob",
                PSG=self.Bob_PSG,
                pol="H",
                channels=self.pso_params.channels34,
                user_ctrl=self.Charlie_EPC2,
            )

            if self.step4_success:
                self.step4_failed_badly = False

            num_tries += 1

        tocBH = time.perf_counter()
        log.info(f"Time taken for Bob H2 Stabilization: {tocBH - ticBH} seconds")
        # time.sleep(5)

        log.info("Bob_H2_Stabilization Completed Successfully")

    async def check_Bob_H2_Stabilization(self):
        """Checks the current visibility or polarization stability for Bob H2"""
        await self.Bob_PSG.polarize("H")

        _ = await OSW_operate(
            [self.Charlie_Rigol1, self.Charlie_Rigol2],
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.Charlie_TimeTagger,
            channels=[1, 2, 3, 4],
            ch1_params=self.ch1_params,
            ch2_params=self.ch2_params,
        )

        Bob_H2_avg_visibility = await calculate_average_visibility(
            device=self.Charlie_TimeTagger,
            channels=self.pso_params.channels34,
            controller=self.Charlie_EPC2,
            voltage=self.step4_best_voltage,
        )

        log.debug(f"Bob H2 visibility: {Bob_H2_avg_visibility}")

        if Bob_H2_avg_visibility <= self.pso_params.threshold_cost2:
            self.step4_success = True
        else:
            self.step4_success = False
            if Bob_H2_avg_visibility <= (1 + self.pso_params.visTol) * self.pso_params.threshold_cost2:
                self.step4_failed_badly = False
            else:
                self.step4_failed_badly = True
        log.info(f"Bob_H2_Stabilization visibility: {'Valid' if self.step5_success else 'Failed'}")

        self.step4_visibility = Bob_H2_avg_visibility
        if self.cb is not None:
            await self.cb({"pol_tracking": {"Bob_H2": self.step4_visibility}})

    async def check_H_stability(self):
        log.info("Tracking H polarization stabilization...")
        await self.check_Bob_H1_Stabilization()
        if not self.step1.success:
            log.info("Step 1: Failed, rerunning Step 1...")
            await self.run_Bob_H1_Stabilization()

        await self.check_Bob_H2_Stabilization()
        if not self.step4.success:
            log.info("Step 4: Failed, rerunning Step 4...")
            await self.run_Bob_H2_Stabilization()

        await self.check_Alice_H1H2_Stabilization()
        if not self.step3.H1_success or not self.step3.H2_success:
            log.info("Step 3: Failed, rerunning Step 3...")
            await self.run_Alice_H1D2_Stabilization()

        log.info("Rechecking H polarization steps...")
        if not self.step1.success or not self.step3.H1_success or not self.step3.H2_success or not self.step4.success:
            log.info("One or more steps failed after checks, rerunning initial stabilization")
            self.initial_stabilization()
        else:
            log.debug("All visibilities are below their respective thresholds\nStabilization is good for now!")

    async def check_D_stability(self):
        log.info("Tracking D polarization stabilization...")
        await self.check_Bob_H1_Stabilization()
        if not self.step1.success:
            log.info("Step 1: Failed, rerunning Step 1...")
            await self.run_Bob_H1_Stabilization()

        await self.check_Bob_D2_Stabilization()
        if not self.step2.success:
            log.info("Step 2: Failed, rerunning Step 2...")
            await self.run_Bob_D2_Stabilization()

        await self.check_Alice_H1D2_Stabilization()
        if not self.step3.success:
            log.info("Step 3: Failed, rerunning Step 3...")
            await self.run_Alice_H1D2_Stabilization()

        await self.check_Bob_H2_Stabilization()
        if not self.step4.success:
            log.info("Step 4: Failed, rerunning Step 4...")
            await self.run_Bob_H2_Stabilization()

        log.info("Rechecking D polarization steps...")  # TODO Maybe add self.stepX.check() for all steps?
        if (
            not self.step1.success
            or not self.step3.H1_success
            or not self.step3.D2_success
            or not self.step4.success
            or not self.step2.success
        ):
            log.info("One or more steps failed after checks, rerunning initial stabilization")
            await self.initial_stabilization()
        else:
            log.info("All visibilities are below their respective thresholds\nStabilization is good for now!")

    async def initial_stabilization(self):
        # NOTE Step.run() does not check whether self.failed_badly is True or False when self.success = False,
        # only Step.check() does this
        log.info("Running Initial Stabilization...")
        await self.run_Bob_H1_Stabilization()
        await self.run_Bob_D2_Stabilization()
        await self.run_Alice_H1D2_Stabilization()
        await self.run_Bob_H2_Stabilization()
        log.info("Initial Stabilization Completed")
        self.only_check()

    async def only_check(self):
        # NOTE Only check, no stablization.
        log.info("Checking Step 1")
        await self.check_Bob_H1_Stabilization()
        await self.check_Alice_H1D2_Stabilization()
        log.info("Checking Step 2")
        await self.check_Bob_D2_Stabilization()
        log.info("Checking Step 3")
        await self.check_Alice_H1D2_Stabilization()
        log.info("Checking Step 4")
        await self.check_Bob_H2_Stabilization()
