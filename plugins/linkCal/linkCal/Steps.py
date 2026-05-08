from PSOManager import PSOManager
import time
import numpy as np


def log_event(log_file, message, log=True):
    """Log the event message to the file and print it."""
    print(message)
    if log is True:
        log_file.write(message + "\n")


def VisibilityCal(channels):  # TODO Maybe add a way to normalize with respect to Alice/Bob zero power?
    return channels[1]/channels[0]


def calculate_user_visibilities(user, device, channels, log_file, controller=None, voltage=None):
    polarization_states = ["H", "V", "D", "A", "L", "R"]
    visibilities = {}

    for state in polarization_states:
        getattr(user.PSG, "polSET")(getattr(user.PSG, state))
        avg_visibility = calculate_average_visibility(device, channels, log_file, controller, voltage)
        visibilities[state] = avg_visibility
        log_event(log_file, f"{user.name} visibility {state}: {avg_visibility}\n")

    return visibilities


def calculate_average_visibility(device, channels, log_file, controller=None, voltage=None, num_measurements=10):
    # Set the voltage on the controller
    if controller is not None and voltage is not None:
        controller.Vset(voltage)

    # Perform measurements and calculate visibility
    total_visibility = 0
    for i in range(num_measurements):
        measure = MeasureFunction(device, channels)
        visibility = VisibilityCal(measure)
        total_visibility += visibility

    # Calculate the average visibility
    average_visibility = total_visibility / num_measurements

    return average_visibility


def MeasureFunction(meas_device, channels, measurement_time=0.01):
    return meas_device.getChannelCountRate(channels, measurement_time=measurement_time)


def OSW_operate(OSW, Alice_Switch_status, Bob_Switch_status, meas_device, channels, log_file, initCheck=False):
    success = False

    OSW_A, OSW_B = OSW

    while True:
        if Alice_Switch_status == 0:
            OSW_A.control_channel(1, "OFF")
        elif Alice_Switch_status == 1:
            OSW_A.configure_channel(**OSW_A.ch1_params)
            OSW_A.control_channel(1, "ON")

        if Bob_Switch_status == 0:
            OSW_B.control_channel(2, "OFF")
        elif Bob_Switch_status == 1:
            OSW_B.configure_channel(**OSW_B.ch2_params)
            OSW_B.control_channel(2, "ON")

        time.sleep(1)

        power_check = np.sum(MeasureFunction(meas_device, channels))

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

            if (
                abs(power_check / reference_power - 1) < 0.9 * 1e9
            ):  # TODO Most likely need to change this tolerance here
                success = True
                break
    return success, power_check


class PSOParams:
    def __init__(self, Measure_CostFunction, log_event, log_file, meas_device):
        self.num_particles = 20
        self.max_iter = 20
        self.threshold_cost1 = 0.02  # 0.015
        self.threshold_cost2 = 0.05  # 0.025
        self.threshold_cost_Alice = 0.08  # 0.045
        self.channels12 = [1, 2]
        self.channels34 = [3, 4]
        self.Measure_CostFunction = Measure_CostFunction  # Assigning function
        self.log_event = log_event  # Assigning function
        self.log_file = log_file
        self.meas_device = meas_device
        self.visTol = 0.015


class Step:
    def __init__(self, name, pso_params, cb=None):
        self.name = name
        self.success = False
        self.pso_params = pso_params
        self.failed_badly = True
        self.cb = cb

        if isinstance(pso_params, dict):  # Checks if pso_params is a dictionary
            for key, value in pso_params.items():
                setattr(self, key, value)
        elif isinstance(pso_params, PSOParams):  # Checks if pso_params is a PSOParams object
            self.num_particles = pso_params.num_particles
            self.max_iter = pso_params.max_iter
            self.threshold_cost1 = pso_params.threshold_cost1
            self.threshold_cost2 = pso_params.threshold_cost2
            self.threshold_cost_Alice = pso_params.threshold_cost_Alice
            self.channels12 = [1, 2]
            self.channels34 = [3, 4]
            self.Measure_CostFunction = pso_params.Measure_CostFunction
            self.log_file = pso_params.log_file
            self.log_event = pso_params.log_event
            self.meas_device = pso_params.meas_device
            self.visTol = (
                pso_params.visTol
            )  # percentage away from threshold_cost that checks how badly stabilization fails
        else:
            raise ValueError(f"pso_params was passed as {type(pso_params)}, accepted types are dict and PSOParams only")

        self.sleep_time = 0.001
        self.MAX_TRIES = 5

        self.best_voltage = None
        self.visibility = -0.001
        self.pso = None

        self.H1_visibility = -0.001
        self.D2_visibility = -0.001
        self.H2_visibility = -0.001

        self.H1_success = False
        self.D2_success = False
        self.H2_success = False

    def setup_PSOManager(self, pso_params, threshold_cost):
        """Sets up a PSOManager object"""
        self.pso = PSOManager(pso_params, threshold_cost)

    def run(self):
        """Runs the step and updates success status"""
        raise NotImplementedError

    def check(self):
        """Checks if the step is still valid and updates success status"""
        raise NotImplementedError

    def reset(self):
        """Reset success state for retry"""
        self.success = False
        self.failed_badly = True


class Bob_H1_Stabilization(Step):
    def check(self):
        """Checks the current visibility or polarization stability for Bob H1"""
        self.log_event(self.log_file, "Setting H polarization from Bob's PSG")
        self.Bob.PSG.polSET(self.Bob.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file)

        Bob_H1_avg_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels12,
            log_file=self.log_file,
            controller=self.Charlie.Pol_CTRL1,
            voltage=self.best_voltage,
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        self.log_event(self.log_file, f"Bob H1 visibility: {Bob_H1_avg_visibility}")
        if self.cb is not None:
            self.cb({"pol_tracking": {"Bob_H1": Bob_H1_avg_visibility}})

        if Bob_H1_avg_visibility <= self.threshold_cost1:
            self.success = True
        else:
            self.success = False
            if Bob_H1_avg_visibility <= (1 + self.visTol) * self.threshold_cost1:
                self.failed_badly = False
            else:
                self.failed_badly = True
        self.log_event(self.log_file, f"{self.name} visibility: {'Valid' if self.success else 'Failed'}")

        self.visibility = Bob_H1_avg_visibility

    def run(self):
        self.reset()
        ticBH = time.perf_counter()
        # log_file = self.log_file # not sure if these are ok to use
        # log_event = self.log_event
        pso = PSOManager(self.pso_params, self.threshold_cost1)
        # setup_PSOManager(self.threshold_cost1)

        self.log_event(self.log_file, f"Running {self.name}...")
        self.log_event(self.log_file, "Setting H polarization from Bob's PSG")
        # self.Bob.PSG.polSET(self.Bob.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],  # TODO Need to check channels for TimeTaggerManager (is 6 given as 2 or 6?)
            log_file=self.log_file,
        )

        num_tries = 0
        # self.check()
        while num_tries <= self.MAX_TRIES and not self.success:
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            self.best_voltage, self.visibility, self.success = pso.optimize_polarization(
                user=self.Bob, pol=self.Bob.PSG.H, channels=self.channels12, user_ctrl=self.Charlie.Pol_CTRL1
            )

            if self.success:
                self.failed_badly = False

            num_tries += 1

        tocBH = time.perf_counter()
        self.log_event(self.log_file, f"Time taken for Bob H1 Stabilization: {tocBH - ticBH} seconds")

        self.log_event(self.log_file, f"{self.name} Completed Successfully")


class Bob_D2_Stabilization(Step):
    def check(self):
        """Checks the current visibility or polarization stability for Bob D2"""
        self.log_event(self.log_file, "Setting D polarization from Bob's PSG")
        self.Bob.PSG.polSET(self.Bob.PSG.D)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )

        Bob_D2_avg_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels34,
            log_file=self.log_file,
            controller=self.Charlie.Pol_CTRL2,
            voltage=self.best_voltage,
        )

        self.log_event(self.log_file, f"Bob D2 visibility: {Bob_D2_avg_visibility}")

        if Bob_D2_avg_visibility <= self.threshold_cost2:
            self.success = True
        else:
            self.success = False
            if Bob_D2_avg_visibility <= (1 + self.visTol) * self.threshold_cost2:
                self.failed_badly = False
            else:
                self.failed_badly = True
        self.log_event(self.log_file, f"{self.name} visibility: {'Valid' if self.success else 'Failed'}")

        self.visibility = Bob_D2_avg_visibility
        if self.cb is not None:
            self.cb({"pol_tracking": {"Bob_D2": Bob_D2_avg_visibility}})

    def run(self):
        self.reset()
        ticBD = time.perf_counter()
        pso = PSOManager(self.pso_params, self.threshold_cost2)

        self.log_event(self.log_file, f"Running {self.name}...")
        self.log_event(self.log_file, "Setting D polarization from Bob's PSG")

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )

        # self.check()
        num_tries = 0
        while num_tries <= self.MAX_TRIES and not self.success:
            # Optimize Charlie Pol_CTRL2 to find best PSO voltage and cost
            self.best_voltage, self.visibility, self.success = pso.optimize_polarization(
                user=self.Bob, pol=self.Bob.PSG.D, channels=self.channels34, user_ctrl=self.Charlie.Pol_CTRL2
            )

            if self.success:
                self.failed_badly = False

            num_tries += 1

        tocBD = time.perf_counter()
        self.log_event(self.log_file, f"Time taken for Bob D2 Stabilization: {tocBD - ticBD} seconds")

        self.log_event(self.log_file, f"{self.name} Completed Successfully")


class Alice_H1D2_Stabilization(Step):
    def check_H1D2(self, cb=None):
        """Checks the current visibility or polarization stability for Alice H1 and D2"""
        self.log_event(self.log_file, "Checking Alice's current visibilities...")
        self.log_event(self.log_file, "Setting H polarization from Alice's PSG")
        self.Alice.PSG.polSET(self.Alice.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )

        self.H1_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels12,
            log_file=self.log_file,
            # It is actually important to add these last two arguments since we are constantly checking
            # if each step is still vaild
            controller=self.Alice.Pol_CTRL1,
            voltage=self.best_voltage,
        )

        self.log_event(self.log_file, f"Setting D polarization from Alice's PSG: best_voltage = {self.best_voltage}")
        self.Alice.PSG.polSET(self.Alice.PSG.D)  # TODO Need to be able to check Alice H2 as well

        self.D2_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels34,
            log_file=self.log_file,
            controller=self.Alice.Pol_CTRL1,
            voltage=self.best_voltage,
        )

        self.Alice_all_visibilities = calculate_user_visibilities(
            user=self.Alice, device=self.meas_device, channels=self.channels12, log_file=self.log_file
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        self.log_event(
            self.log_file, f"Alice H1 visibility: {self.H1_visibility}, Alice D2 visibility: {self.D2_visibility}"
        )

        if self.H1_visibility <= self.threshold_cost_Alice:
            self.H1_success = True
            self.H1_failed_badly = False
        else:
            self.H1_success = False
            if self.H1_visibility <= (1 + self.visTol) * self.threshold_cost_Alice:
                self.H1_failed_badly = False
            else:
                self.H1_failed_badly = True

        if self.D2_visibility <= self.threshold_cost_Alice:
            self.D2_success = True
            self.D2_failed_badly = False
        else:
            self.D2_success = False
            if self.D2_visibility <= (1 + self.visTol) * self.threshold_cost_Alice:
                self.D2_failed_badly = False
            else:
                self.D2_failed_badly = True

        self.success = self.H1_success and self.D2_success
        self.failed_badly = self.H1_failed_badly or self.D2_failed_badly
        self.log_event(self.log_file, f"{self.name} visibility: {'Valid' if self.success else 'Failed'}")

        self.D2_visibility = self.D2_visibility
        if self.cb is not None:
            self.cb({"pol_tracking": {"Alice_H1": self.H1_visibility, "Alice_D2": self.D2_visibility}})

    def check_H1H2(self):
        """Checks the current visibility or polarization stability for Alice H1 and H2"""
        self.log_event(self.log_file, "Checking Alice's current visibilities...")
        self.log_event(self.log_file, "Setting H polarization from Alice's PSG")
        self.Alice.PSG.polSET(self.Alice.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=1,
            Bob_Switch_status=0,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )
        self.H1_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels12,
            log_file=self.log_file,
            # It is actually important to add these last two arguments since we are constantly checking
            # if each step is still vaild
            controller=self.Alice.Pol_CTRL1,
            voltage=self.best_voltage,
        )

        # self.log_event(self.log_file, "Setting D polarization from Alice's PSG")
        # self.Alice.PSG.polSET(self.Alice.PSG.D) # TODO Need to be able to check Alice H2 as well

        self.H2_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels34,
            log_file=self.log_file,
            controller=self.Alice.Pol_CTRL1,
            voltage=self.best_voltage,
        )

        self.Alice_all_visibilities = calculate_user_visibilities(
            user=self.Alice, device=self.meas_device, channels=self.channels12, log_file=self.log_file
        )

        # Bob_visibilities_dict[f"Run {i}, Step 1: (Avg) Bob H"] = Bob_H_avg_visibility
        self.log_event(
            self.log_file, f"Alice H1 visibility: {self.H1_visibility}, Alice H2 visibility: {self.H2_visibility}"
        )

        if self.H1_visibility <= self.threshold_cost_Alice:
            self.H1_success = True
            self.H1_failed_badly = False
        else:
            self.H1_success = False
            if self.H1_visibility <= (1 + self.visTol) * self.threshold_cost_Alice:
                self.H1_failed_badly = False
            else:
                self.H1_failed_badly = True

        if self.H2_visibility <= self.threshold_cost_Alice:
            self.H2_success = True
            self.H2_failed_badly = False
        else:
            self.H2_success = False
            if self.H2_visibility <= (1 + self.visTol) * self.threshold_cost_Alice:
                self.H2_failed_badly = False
            else:
                self.H2_failed_badly = True

        self.success = self.H1_success and self.H2_success
        self.failed_badly = self.H1_failed_badly or self.H2_failed_badly
        self.log_event(self.log_file, f"{self.name} visibility: {'Valid' if self.success else 'Failed'}")

        if self.cb is not None:
            self.cb({"pol_tracking": {"Alice_H1": self.H1_visibility, "Alice_H2": self.H2_visibility}})

    def run(self, step1, step2):
        self.reset()  # Automatically sets self.success to False and self.failed_badly to True
        MAX_TRIES = 10
        num_tries = 0
        ticAH = time.perf_counter()
        pso = PSOManager(self.pso_params, self.threshold_cost_Alice)

        self.log_event(self.log_file, f"Running {self.name}...")
        self.log_event(self.log_file, "Setting H polarization from Alice's PSG")

        # log_event(log_file, f"Running {self.name}... Checking Step 1 and Step 2 validity")

        all_succeeded = self.success and step1.success and step2.success
        while num_tries < MAX_TRIES and not all_succeeded:
            num_tries += 1
            _ = OSW_operate(
                self.Charlie.Rigols,
                Alice_Switch_status=1,
                Bob_Switch_status=0,
                meas_device=self.meas_device,
                channels=[1, 2, 3, 4],
                log_file=self.log_file,
            )
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            if step1.success and step2.success and (not self.H1_success or not self.D2_success):
                self.best_voltage, self.best_cost, self.success = pso.optimize_polarization(
                    user=self.Alice, pol=self.Alice.PSG.H, channels=self.channels12, user_ctrl=self.Alice.Pol_CTRL1
                )

            # Revalidate Step 1 and Step 2
            step1.check()
            step2.check()

            # Retry only failed steps
            if not step1.success:
                log_event(self.log_file, "Step 1 visibilites are above threshold, Redoing Step 1")
                step1.run()
            if not step2.success:
                log_event(self.log_file, "Step 2 visibilites are above threshold, Redoing Step 2")
                step2.run()

            self.check_H1D2()

            self.log_event(
                self.log_file, f"Alice H1 visibility: {self.H1_visibility}, Alice D2 visibility: {self.D2_visibility}"
            )

            for key in self.Alice_all_visibilities.keys():
                self.log_event(self.log_file, f"Alice {key} visibility: {self.Alice_all_visibilities[key]}")

            # if step1_valid and step2_valid:
            #     break # If both are valid, Step 3 is successful

            all_succeeded = self.success and step1.success and step2.success  # TODO fix this to only deal with Alice

        if num_tries == MAX_TRIES:
            raise TimeoutError(f"Alice H1 and D2 failed to stabilize within {num_tries} tries")

        if self.success:
            self.failed_badly = False

        log_event(self.log_file, f"{self.name} Completed Successfully")
        tocAH = time.perf_counter()
        self.log_event(self.log_file, f"Time taken for Alice H1 and D2 Stabilization: {tocAH - ticAH} seconds")


class Bob_H2_Stabilization(Step):
    def check(self):
        """Checks the current visibility or polarization stability for Bob H2"""
        self.Bob.PSG.polSET(self.Bob.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )

        Bob_H2_avg_visibility = calculate_average_visibility(
            device=self.meas_device,
            channels=self.channels34,
            log_file=self.log_file,
            controller=self.Charlie.Pol_CTRL2,
            voltage=self.best_voltage,
        )

        self.log_event(self.log_file, f"Bob H2 visibility: {Bob_H2_avg_visibility}")

        if Bob_H2_avg_visibility <= self.threshold_cost2:
            self.success = True
        else:
            self.success = False
            if Bob_H2_avg_visibility <= (1 + self.visTol) * self.threshold_cost2:
                self.failed_badly = False
            else:
                self.failed_badly = True
        self.log_event(self.log_file, f"{self.name} visibility: {'Valid' if self.success else 'Failed'}")

        self.visibility = Bob_H2_avg_visibility
        if self.cb is not None:
            self.cb({"pol_tracking": {"Bob_H2": self.visibility}})

    def run(self):
        self.reset()
        ticBH = time.perf_counter()
        pso = PSOManager(self.pso_params, self.threshold_cost2)

        self.log_event(self.log_file, f"Running {self.name}...")
        self.log_event(self.log_file, "Setting H polarization from Bob's PSG")
        # self.Bob.PSG.polSET(self.Bob.PSG.H)

        _ = OSW_operate(
            self.Charlie.Rigols,
            Alice_Switch_status=0,
            Bob_Switch_status=1,
            meas_device=self.meas_device,
            channels=[1, 2, 3, 4],
            log_file=self.log_file,
        )

        # self.check()
        num_tries = 0
        while num_tries <= self.MAX_TRIES and not self.success:
            # Optimize Charlie Pol_CTRL1 to find best PSO voltage and cost
            self.best_voltage, self.visibility, self.success = pso.optimize_polarization(
                user=self.Bob, pol=self.Bob.PSG.H, channels=self.channels34, user_ctrl=self.Charlie.Pol_CTRL2
            )

            if self.success:
                self.failed_badly = False

            num_tries += 1

        tocBH = time.perf_counter()
        self.log_event(self.log_file, f"Time taken for Bob H2 Stabilization: {tocBH - ticBH} seconds")
        # time.sleep(5)

        self.log_event(self.log_file, f"{self.name} Completed Successfully")
