import logging
import numpy as np
from .IM_manager import IM_Manager
from .Attn_manager import Attenuation_Manager
from linkCal.interpreter.PSO.PSO import PSO
from linkCal.interpreter.PSO.utility import MeasurementType

log = logging.getLogger(__name__)


class QFCWorkflow:
    """
    Complete QFC experimental workflow based on BSM_running_100k.py:

    1. Polarization stabilization (PSO)
    2. HOM measurement with stabilization light
    3. BSM measurement with stabilization light
    4. QFC initialization (IM optimization + mean photon number)
    5. HOM with QFC (EOM-based, 20ns pulses)
    6. BSM with QFC

    Supports both Alice and Bob QFC operations.
    """

    def __init__(self, hal, msgclient, agent_comms_topic, cb=None):
        """
        Initialize QFC workflow with HAL and messaging infrastructure.

        Parameters
        ----------
        hal : HAL
            Hardware Abstraction Layer instance
        msgclient : MessageClient
            Message client for publishing data
        agent_comms_topic : str
            Topic for agent communications
        cb : callable, optional
            Callback function for publishing data
        """
        self.hal = hal
        self.msgclient = msgclient
        self.agent_comms_topic = agent_comms_topic
        self.cb = cb

        # Device references
        self.Alice = None
        self.Bob = None
        self.Charlie = None

        # QFC managers for Alice and Bob
        self.Alice_QFC = None
        self.Bob_QFC = None

        # Workflow state
        self.cancelled = False
        self.pol_stabilized = False
        self.hom_checked = False
        self.bsm_checked = False
        self.qfc_initialized = False

        # PSO and BSM instances
        self.PSO = None
        self.BSM = None

        log.info("QFC Workflow instance created")

    async def init_devices(self):
        """Initialize all devices from HAL."""
        log.info("Initializing devices for QFC workflow...")

        # Get Alice's devices
        self.Alice = type(
            "Alice",
            (),
            {
                "name": "Alice",
                "Rigol1": self.hal.devs.get("alice-rigol1"),  # DC Rigol
                "Rigol2": self.hal.devs.get("alice-rigol2"),  # RF Rigol
                "PSG": self.hal.devs.get("alice-psg"),
                "Pol_CTRL1": self.hal.devs.get("alice-epc"),
            },
        )()

        # Get Bob's devices
        self.Bob = type(
            "Bob",
            (),
            {
                "name": "Bob",
                "Rigol1": self.hal.devs.get("bob-rigol1"),  # DC Rigol
                "Rigol2": self.hal.devs.get("bob-rigol2"),  # RF Rigol
                "PSG": self.hal.devs.get("bob-psg"),
            },
        )()

        # Get Charlie's devices (measurement)
        self.Charlie = type(
            "Charlie",
            (),
            {
                "name": "Charlie",
                "Rigols": (self.Alice.Rigol2, self.Bob.Rigol2),  # For switching
                "TimeTaggerManager": self.hal.devs.get("charlie-timetagger"),
                "Pol_CTRL1": self.hal.devs.get("charlie-polctrl1"),
                "Pol_CTRL2": self.hal.devs.get("charlie-polctrl2"),
            },
        )()

        # Connect all devices
        await self._connect_devices()

        # Initialize QFC managers
        await self._init_qfc_managers()

        log.info("All devices initialized successfully")

    async def _connect_devices(self):
        """Connect all devices."""
        devices_to_connect = [
            self.Alice.Rigol1,
            self.Alice.Rigol2,
            self.Alice.PSG,
            self.Alice.Pol_CTRL1,
            self.Bob.Rigol1,
            self.Bob.Rigol2,
            self.Bob.PSG,
            self.Charlie.TimeTaggerManager,
            self.Charlie.Pol_CTRL1,
            self.Charlie.Pol_CTRL2,
        ]

        for dev in devices_to_connect:
            if dev and hasattr(dev, "connect"):
                await dev.connect()

    async def _init_qfc_managers(self):
        """Initialize IM and Attenuation managers for Alice and Bob."""
        # Alice QFC managers
        self.Alice_QFC = type(
            "AliceQFC",
            (),
            {
                "IM": IM_Manager(dc_src=self.Alice.Rigol1, rf_src=self.Alice.Rigol2),
                "Attn": Attenuation_Manager(dc_src=self.Alice.Rigol1),
                "user_Chlist": [1],
            },
        )()

        # Bob QFC managers
        self.Bob_QFC = type(
            "BobQFC",
            (),
            {
                "IM": IM_Manager(dc_src=self.Bob.Rigol1, rf_src=self.Bob.Rigol2),
                "Attn": Attenuation_Manager(dc_src=self.Bob.Rigol1),
                "user_Chlist": [2],
            },
        )()

    async def _init_pso_bsm(self):
        """Initialize PSO and BSM instances for Step 2."""
        # Import BSM class
        from bsm.bsm.interpreter.BSM import BSM as BSMClass

        # Initialize PSO instance
        self.PSO = PSO(self.hal, self.agent_comms_topic, cb=self.cb)
        await self.PSO.init()

        # Initialize BSM instance (reuse from BSM.py)
        self.BSM = BSMClass(self.hal, self.msgclient, self.agent_comms_topic)
        await self.BSM.init_device()

        log.info("PSO and BSM instances initialized")

    async def polarization_stabilization(self):
        """
        Step 1: Polarization Stabilization using PSO

        Runs the 4-step PSO stabilization:
        1. Bob H1 stabilization
        2. Bob D2 stabilization
        3. Alice H1D2 stabilization
        4. Bob H2 stabilization
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 1: POLARIZATION STABILIZATION (PSO)")
        log.info("=" * 80)

        # Run initial stabilization
        await self.PSO.initial_stabilization()

        self.pol_stabilized = True
        log.info("\nPolarization Stabilization Complete!")

        await self.__publish(
            {
                "polarization_stabilization": {
                    "status": "complete",
                    "bob_h1_visibility": self.PSO.step1_visibility,
                    "bob_d2_visibility": self.PSO.step2_visibility,
                    "alice_h1_visibility": self.PSO.H1_visibility,
                    "alice_d2_visibility": self.PSO.D2_visibility,
                    "bob_h2_visibility": self.PSO.step4_visibility,
                }
            }
        )

    async def hom_with_stabilization_light(self, start_delay=0, end_delay=10000, step_delay=500):
        """
        Step 2: HOM measurement with stabilization light (AOM-based)

        Scans AOM burst delay to find HOM dip.
        Uses 80MHz burst mode with ~100ns pulses.

        Parameters
        ----------
        start_delay : int
            Starting delay in nanoseconds
        end_delay : int
            Ending delay in nanoseconds
        step_delay : int
            Delay step in nanoseconds

        Returns
        -------
        dict
            HOM measurement results including visibility and best delay
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 2: HOM WITH STABILIZATION LIGHT")
        log.info("=" * 80)

        # Use BSM.HOM_time_scan method
        hom_results = await self.BSM.HOM_time_scan()

        self.hom_checked = True
        log.info(f"\nHOM visibility: {hom_results['visibility']:.4f}")
        log.info(f"Best delay: {hom_results['best_delay_ns']*1e9:.1f} ns")

        await self.__publish({"hom_stabilization_light": hom_results})

        return hom_results

    async def bsm_with_stabilization_light(self, measurement_time=1):
        """
        Step 3: BSM measurement with stabilization light

        Measures both psi+ and psi- Bell states to verify entanglement quality.

        Parameters
        ----------
        measurement_time : float
            Measurement time in seconds

        Returns
        -------
        dict
            BSM measurement results including visibilities and error rates
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 3: BSM WITH STABILIZATION LIGHT")
        log.info("=" * 80)

        # Measure psi+
        log.info("\n--- Measuring Psi+ ---")
        psi_p_plus, psi_m_plus = await self.BSM.BSM_psi_plus(measurement_time)
        vis_plus = (psi_p_plus - psi_m_plus) / psi_p_plus if psi_p_plus > 0 else 0

        # Measure psi-
        log.info("\n--- Measuring Psi- ---")
        psi_p_minus, psi_m_minus = await self.BSM.BSM_psi_minus(measurement_time)
        vis_minus = (psi_m_minus - psi_p_minus) / psi_m_minus if psi_m_minus > 0 else 0

        # Calculate error rates
        # Z-basis error: should see psi+ in (1,2) and (3,4)
        z_error = psi_m_plus / (psi_p_plus + psi_m_plus) if (psi_p_plus + psi_m_plus) > 0 else 1.0

        # X-basis error: should see psi- in (1,4) and (2,3)
        x_error = psi_p_minus / (psi_p_minus + psi_m_minus) if (psi_p_minus + psi_m_minus) > 0 else 1.0

        self.bsm_checked = True

        log.info(f"\nPsi+ visibility: {vis_plus:.4f}")
        log.info(f"Psi- visibility: {vis_minus:.4f}")
        log.info(f"Z-basis error rate: {z_error:.4f}")
        log.info(f"X-basis error rate: {x_error:.4f}")

        bsm_results = {
            "psi_plus": {"psi_p": float(psi_p_plus), "psi_m": float(psi_m_plus), "visibility": float(vis_plus)},
            "psi_minus": {"psi_p": float(psi_p_minus), "psi_m": float(psi_m_minus), "visibility": float(vis_minus)},
            "error_rates": {"z_error": float(z_error), "x_error": float(x_error)},
        }

        await self.__publish({"bsm_stabilization_light": bsm_results})

        return bsm_results

    async def qfc_initialization(self, target_mu=0.05):
        """
        Step 4: QFC Initialization
        - Optimize IM DC bias for Alice and Bob
        - Set mean photon number for Alice and Bob

        Parameters
        ----------
        target_mu : float
            Target mean photon number (default 0.05)
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 4: QFC INITIALIZATION")
        log.info("=" * 80)

        # Turn OFF AOM (Ch1), Turn ON IM (Ch2)
        log.info("Switching from AOM to IM...")
        await self._switch_to_im_mode()

        # 4.1: Optimize Alice IM DC bias
        log.info("\n--- Optimizing Alice IM DC bias ---")
        await self._set_light_source(alice_on=True, bob_on=False)
        await self.Alice_QFC.IM.optimize_dc_bias(
            timetagger=self.Charlie.TimeTaggerManager, Chlist=self.Alice_QFC.user_Chlist
        )

        # 4.2: Optimize Bob IM DC bias
        log.info("\n--- Optimizing Bob IM DC bias ---")
        await self._set_light_source(alice_on=False, bob_on=True)
        await self.Bob_QFC.IM.optimize_dc_bias(
            timetagger=self.Charlie.TimeTaggerManager, Chlist=self.Bob_QFC.user_Chlist
        )

        # 4.3: Set Alice mean photon number
        log.info("\n--- Setting Alice mean photon number ---")
        await self._set_light_source(alice_on=False, bob_on=False)  # Bob light OFF
        best_v_Alice = await self._set_mean_photon_number(user_qfc=self.Alice_QFC, target_mu=target_mu)

        # 4.4: Set Bob mean photon number
        log.info("\n--- Setting Bob mean photon number ---")
        await self._set_light_source(alice_on=False, bob_on=False)  # Alice light OFF
        best_v_Bob = await self._set_mean_photon_number(user_qfc=self.Bob_QFC, target_mu=target_mu)

        # Restore Alice attenuation
        if hasattr(self.Alice.Rigol1, "set"):
            await self.Alice.Rigol1.set(channel=1, setting="OFFSET", value=best_v_Alice)

        self.qfc_initialized = True
        log.info("\nQFC Initialization Complete!")

        await self.__publish(
            {
                "qfc_initialization": {
                    "status": "complete",
                    "alice_im_voltage": self.Alice_QFC.IM.dc_optimized_v,
                    "bob_im_voltage": self.Bob_QFC.IM.dc_optimized_v,
                    "alice_attn_voltage": best_v_Alice,
                    "bob_attn_voltage": best_v_Bob,
                    "target_mu": target_mu,
                }
            }
        )

    async def _switch_to_im_mode(self):
        """Switch from AOM (Ch1) to IM (Ch2) for both Alice and Bob."""
        # Alice: AOM OFF, IM ON
        if hasattr(self.Alice.Rigol2, "set"):
            await self.Alice.Rigol2.set(channel=1, setting="OFF")
            await self.Alice.Rigol2.set(channel=2, setting="ON")

        # Bob: AOM OFF, IM ON
        if hasattr(self.Bob.Rigol2, "set"):
            await self.Bob.Rigol2.set(channel=1, setting="OFF")
            await self.Bob.Rigol2.set(channel=2, setting="ON")

    async def _set_light_source(self, alice_on=False, bob_on=False):
        """
        Control light sources via AOM DC offset.

        Parameters
        ----------
        alice_on : bool
            True = Alice light ON (offset=1V), False = OFF (offset=5V)
        bob_on : bool
            True = Bob light ON (offset=1V), False = OFF (offset=5V)
        """
        alice_offset = 1.0 if alice_on else 5.0
        bob_offset = 1.0 if bob_on else 5.0

        if hasattr(self.Alice.Rigol1, "set"):
            await self.Alice.Rigol1.set(channel=1, setting="OFFSET", value=alice_offset)

        if hasattr(self.Bob.Rigol1, "set"):
            await self.Bob.Rigol1.set(channel=1, setting="OFFSET", value=bob_offset)

    async def _set_mean_photon_number(self, user_qfc, target_mu=0.05):
        """
        Set mean photon number by optimizing attenuation.

        Parameters
        ----------
        user_qfc : object
            User's QFC manager (Alice_QFC or Bob_QFC)
        target_mu : float
            Target mean photon number

        Returns
        -------
        float
            Optimized attenuation voltage
        """
        # Convert mu to count rate (assuming 10MHz rep rate, 50% efficiency)
        target_counts = target_mu * 10e6 * 0.5

        await user_qfc.Attn.optimize_attenuation(
            timetagger=self.Charlie.TimeTaggerManager, target=target_counts, Chlist=user_qfc.user_Chlist
        )

        return user_qfc.Attn.attn_optimized_v

    async def hom_eom_scan(self, start_phase=0, end_phase=360, step_phase=10):
        """
        Step 5: HOM measurement with QFC (EOM-based, 20ns pulses)

        Scans IM phase instead of AOM burst delay.
        Uses 10MHz square wave with 20% duty cycle for 20ns pulses.

        Parameters
        ----------
        start_phase : int
            Starting phase in degrees
        end_phase : int
            Ending phase in degrees
        step_phase : int
            Phase step in degrees
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 5: HOM WITH QFC (EOM-BASED)")
        log.info("=" * 80)

        # Set polarizations
        if hasattr(self.Alice.PSG, "set_polarization"):
            await self.Alice.PSG.set_polarization("H")
        if hasattr(self.Bob.PSG, "set_polarization"):
            await self.Bob.PSG.set_polarization("H")

        # Turn off AOM channels
        await self._turn_off_aom()

        # Configure IM for 20ns pulses (10MHz, 20% duty cycle)
        await self._configure_im_for_hom()

        # Scan phase
        phases = np.arange(start_phase, end_phase, step_phase)
        coincidences_data = []
        coincidences_data_norm = []

        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]

        for phase in phases:
            if self.cancelled:
                break

            # Set IM phase on Bob's Rigol
            if hasattr(self.Bob.Rigol2, "set"):
                await self.Bob.Rigol2.set(channel=2, setting="PHASE", value=phase)

            # Measure coincidences
            coincidence_hist = await self.Charlie.TimeTaggerManager.measure(
                MeasurementType.COINCIDENCE,
                channel_pairs=channel_pairs,
                measurement_time=1.0,
                binwidth=1e3,  # 1ns binwidth
                n_bins=100,
            )

            # Measure single counts for normalization
            single_counts = await self.Charlie.TimeTaggerManager.measure(
                MeasurementType.RATE, channels=[1, 3], measurement_time=0.1
            )

            # HOM dip is on channel pair (1,3)
            HOM_coincidence_hist = coincidence_hist[1]  # Index for (1,3)
            HOM_coincidence_hist_norm = HOM_coincidence_hist / (single_counts[0] * single_counts[1])

            coincidences_data.append(np.sum(HOM_coincidence_hist))
            coincidences_data_norm.append(np.sum(HOM_coincidence_hist_norm))

            log.info(
                f"Phase: {phase}°, Coincidences: {np.sum(HOM_coincidence_hist)}, "
                f"Normalized: {np.sum(HOM_coincidence_hist_norm)}"
            )

        # Calculate HOM visibility
        HOM_vis = (np.mean(np.sort(coincidences_data)[-3:]) - min(coincidences_data)) / np.mean(
            np.sort(coincidences_data)[-3:]
        )
        HOM_vis_norm = (np.mean(np.sort(coincidences_data_norm)[-3:]) - min(coincidences_data_norm)) / np.mean(
            np.sort(coincidences_data_norm)[-3:]
        )

        log.info(f"\nHOM visibility: {HOM_vis:.4f}")
        log.info(f"HOM visibility (normalized): {HOM_vis_norm:.4f}")

        # Find best phase
        best_phase_idx = np.argmin(coincidences_data)
        best_phase = phases[best_phase_idx]
        log.info(f"Best phase: {best_phase}°")

        await self.__publish(
            {
                "hom_eom_scan": {
                    "visibility": float(HOM_vis),
                    "visibility_normalized": float(HOM_vis_norm),
                    "best_phase": float(best_phase),
                    "phases": phases.tolist(),
                    "coincidences": coincidences_data,
                    "coincidences_normalized": coincidences_data_norm,
                }
            }
        )

        return HOM_vis, best_phase

    async def _turn_off_aom(self):
        """Turn off AOM channels (Ch1) for both Alice and Bob."""
        if hasattr(self.Alice.Rigol2, "set"):
            await self.Alice.Rigol2.set(channel=1, setting="OFF")
        if hasattr(self.Bob.Rigol2, "set"):
            await self.Bob.Rigol2.set(channel=1, setting="OFF")

    async def _configure_im_for_hom(self):
        """Configure IM (Ch2) for HOM measurement: 10MHz square wave, 20% duty cycle."""
        # Alice IM configuration
        if hasattr(self.Alice.Rigol2, "configure"):
            await self.Alice.Rigol2.configure(
                channel=2, mode="square", frequency=10e6, amplitude=5.0, offset=0.0, duty_cycle=20.0, phase=90.0
            )

        # Bob IM configuration
        if hasattr(self.Bob.Rigol2, "configure"):
            await self.Bob.Rigol2.configure(
                channel=2,
                mode="square",
                frequency=10e6,
                amplitude=5.0,
                offset=0.0,
                duty_cycle=20.0,
                phase=0.0,  # Will be scanned
            )

    async def bsm_with_qfc(self, measurement_time=1):
        """
        Step 6: BSM measurement with QFC (20ns pulses from IM)

        Measures both psi+ and psi- Bell states with QFC-converted photons.
        Uses IM-generated 20ns pulses instead of AOM pulses.

        Parameters
        ----------
        measurement_time : float
            Measurement time in seconds

        Returns
        -------
        dict
            BSM measurement results with QFC including visibilities and error rates
        """
        log.info("\n" + "=" * 80)
        log.info("STEP 6: BSM WITH QFC")
        log.info("=" * 80)

        # Ensure IM is configured for BSM (same as HOM configuration)
        await self._configure_im_for_hom()

        # Set polarizations for psi+
        log.info("\n--- Measuring Psi+ with QFC ---")
        if hasattr(self.Alice.PSG, "set_polarization"):
            await self.Alice.PSG.set_polarization("D")
        if hasattr(self.Bob.PSG, "set_polarization"):
            await self.Bob.PSG.set_polarization("D")

        # Measure coincidences for psi+
        channel_pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
        coincidence_hist_plus = await self.Charlie.TimeTaggerManager.measure(
            MeasurementType.COINCIDENCE,
            channel_pairs=channel_pairs,
            measurement_time=measurement_time,
            binwidth=0.1e6,
            n_bins=100,
        )

        psi_p_plus = np.sum(coincidence_hist_plus[0]) + np.sum(coincidence_hist_plus[5])  # (1,2) + (3,4)
        psi_m_plus = np.sum(coincidence_hist_plus[2]) + np.sum(coincidence_hist_plus[3])  # (1,4) + (2,3)
        vis_plus = (psi_p_plus - psi_m_plus) / psi_p_plus if psi_p_plus > 0 else 0

        log.info(f"Psi+ with QFC: psi_p={psi_p_plus}, psi_m={psi_m_plus}, visibility={vis_plus:.4f}")

        # Set polarizations for psi-
        log.info("\n--- Measuring Psi- with QFC ---")
        if hasattr(self.Alice.PSG, "set_polarization"):
            await self.Alice.PSG.set_polarization("D")
        if hasattr(self.Bob.PSG, "set_polarization"):
            await self.Bob.PSG.set_polarization("A")

        # Measure coincidences for psi-
        coincidence_hist_minus = await self.Charlie.TimeTaggerManager.measure(
            MeasurementType.COINCIDENCE,
            channel_pairs=channel_pairs,
            measurement_time=measurement_time,
            binwidth=0.1e6,
            n_bins=100,
        )

        psi_p_minus = np.sum(coincidence_hist_minus[0]) + np.sum(coincidence_hist_minus[5])  # (1,2) + (3,4)
        psi_m_minus = np.sum(coincidence_hist_minus[2]) + np.sum(coincidence_hist_minus[3])  # (1,4) + (2,3)
        vis_minus = (psi_m_minus - psi_p_minus) / psi_m_minus if psi_m_minus > 0 else 0

        log.info(f"Psi- with QFC: psi_p={psi_p_minus}, psi_m={psi_m_minus}, visibility={vis_minus:.4f}")

        # Calculate error rates with QFC
        z_error_qfc = psi_m_plus / (psi_p_plus + psi_m_plus) if (psi_p_plus + psi_m_plus) > 0 else 1.0
        x_error_qfc = psi_p_minus / (psi_p_minus + psi_m_minus) if (psi_p_minus + psi_m_minus) > 0 else 1.0

        log.info(f"\nZ-basis error rate with QFC: {z_error_qfc:.4f}")
        log.info(f"X-basis error rate with QFC: {x_error_qfc:.4f}")

        bsm_qfc_results = {
            "psi_plus": {"psi_p": float(psi_p_plus), "psi_m": float(psi_m_plus), "visibility": float(vis_plus)},
            "psi_minus": {"psi_p": float(psi_p_minus), "psi_m": float(psi_m_minus), "visibility": float(vis_minus)},
            "error_rates": {"z_error": float(z_error_qfc), "x_error": float(x_error_qfc)},
        }

        await self.__publish({"bsm_with_qfc": bsm_qfc_results})

        return bsm_qfc_results

    async def check_stabilization_parameters(self, pol_params=None, hom_params=None, bsm_params=None):
        """
        Check if polarization, HOM, and BSM are within acceptable thresholds before QFC.

        Parameters
        ----------
        pol_params : dict, optional
            Polarization parameters for Alice and Bob
        hom_params : dict, optional
            HOM parameters
        bsm_params : dict, optional
            BSM parameters

        Returns
        -------
        dict
            Check results
        """
        log.info("\n" + "=" * 80)
        log.info("CHECKING STABILIZATION PARAMETERS")
        log.info("=" * 80)

        results = {"polarization": None, "hom": None, "bsm": None, "ready_for_qfc": False}

        # Check polarization (H > 98%, D > 97%)
        if pol_params:
            pol_check = await self._check_polarization(pol_params)
            results["polarization"] = pol_check

        # Check HOM (visibility > 45%)
        if hom_params:
            hom_check = await self._check_hom(hom_params)
            results["hom"] = hom_check

        # Check BSM (Z error < 5%, X error < 30%)
        if bsm_params:
            bsm_check = await self._check_bsm(bsm_params)
            results["bsm"] = bsm_check

        # Overall check
        results["ready_for_qfc"] = all(
            [
                results["polarization"] if pol_params else True,
                results["hom"] if hom_params else True,
                results["bsm"] if bsm_params else True,
            ]
        )

        await self.__publish({"stabilization_check": results})

        return results

    async def _check_polarization(self, pol_params):
        """Check polarization stabilization (H > 98%, D > 97%)."""
        H_threshold = 0.98
        D_threshold = 0.97

        alice_ok = pol_params.get("alice_H", 0) >= H_threshold and pol_params.get("alice_D", 0) >= D_threshold
        bob_ok = pol_params.get("bob_H", 0) >= H_threshold and pol_params.get("bob_D", 0) >= D_threshold

        return alice_ok and bob_ok

    async def _check_hom(self, hom_params):
        """Check HOM visibility > 45%."""
        return hom_params.get("visibility", 0) >= 0.45

    async def _check_bsm(self, bsm_params):
        """Check BSM error rates (Z < 5%, X < 30%)."""
        z_ok = bsm_params.get("z_error", 1.0) <= 0.05
        x_ok = bsm_params.get("x_error", 1.0) <= 0.30
        return z_ok and x_ok

    async def run_full_workflow(self, target_mu=0.05):
        """
        Run the complete QFC workflow as per BSM_running_100k.py:

        Step 1: Polarization stabilization (PSO)
        Step 2: HOM measurement with stabilization light
        Step 3: BSM measurement with stabilization light
        Step 4: QFC initialization (IM optimization + mean photon number)
        Step 5: HOM with QFC (EOM-based, 20ns pulses)
        Step 6: BSM with QFC
        Step 7: Final validation and comparison

        Parameters
        ----------
        target_mu : float
            Target mean photon number for QFC (default 0.05)
        """
        log.info("\n" + "=" * 80)
        log.info("STARTING FULL QFC WORKFLOW")
        log.info("=" * 80)

        # Step 0: Initialize devices
        await self.init_devices()
        await self._init_pso_bsm()

        # Step 1: Polarization Stabilization
        await self.polarization_stabilization()

        # Step 2: HOM with stabilization light
        hom_stab_results = await self.hom_with_stabilization_light()

        # Step 3: BSM with stabilization light
        bsm_stab_results = await self.bsm_with_stabilization_light()

        # Check if ready for QFC
        pol_params = {
            "alice_H": 1.0 - self.PSO.H1_visibility,  # Convert visibility to fidelity
            "alice_D": 1.0 - self.PSO.D2_visibility,
            "bob_H": 1.0 - self.PSO.step1_visibility,
            "bob_D": 1.0 - self.PSO.step2_visibility,
        }
        hom_params = {"visibility": hom_stab_results["visibility"]}
        bsm_params = {
            "z_error": bsm_stab_results["error_rates"]["z_error"],
            "x_error": bsm_stab_results["error_rates"]["x_error"],
        }

        check_results = await self.check_stabilization_parameters(
            pol_params=pol_params, hom_params=hom_params, bsm_params=bsm_params
        )

        if not check_results["ready_for_qfc"]:
            log.warning("\n⚠ System not ready for QFC operation!")
            log.warning("Please check polarization, HOM, and BSM parameters")
            return

        log.info("\n✓ System ready for QFC operation")

        # Step 4: QFC initialization
        await self.qfc_initialization(target_mu=target_mu)

        # Step 5: HOM with QFC
        hom_qfc_vis, best_phase = await self.hom_eom_scan()

        # Validate HOM with QFC
        if hom_qfc_vis >= 0.45:
            log.info(f"\n✓ HOM visibility with QFC {hom_qfc_vis:.4f} is sufficient")
        else:
            log.warning(f"\n✗ HOM visibility with QFC {hom_qfc_vis:.4f} is below threshold (0.45)")
            log.warning("Check laser lock and wavelength matching")

        # Step 6: BSM with QFC
        bsm_qfc_results = await self.bsm_with_qfc()

        # Step 7: Final validation and comparison
        log.info("\n" + "=" * 80)
        log.info("STEP 7: FINAL VALIDATION AND COMPARISON")
        log.info("=" * 80)

        log.info("\n--- Comparison: Stabilization Light vs QFC ---")
        log.info(f"HOM visibility (stab light): {hom_stab_results['visibility']:.4f}")
        log.info(f"HOM visibility (QFC):        {hom_qfc_vis:.4f}")
        log.info(f"BSM Z-error (stab light):    {bsm_stab_results['error_rates']['z_error']:.4f}")
        log.info(f"BSM Z-error (QFC):           {bsm_qfc_results['error_rates']['z_error']:.4f}")
        log.info(f"BSM X-error (stab light):    {bsm_stab_results['error_rates']['x_error']:.4f}")
        log.info(f"BSM X-error (QFC):           {bsm_qfc_results['error_rates']['x_error']:.4f}")

        # Check if QFC performance is acceptable
        qfc_performance_ok = (
            hom_qfc_vis >= 0.45
            and bsm_qfc_results["error_rates"]["z_error"] <= 0.10  # Relaxed threshold for QFC
            and bsm_qfc_results["error_rates"]["x_error"] <= 0.35
        )

        if qfc_performance_ok:
            log.info("\n✓ QFC performance is acceptable for quantum communication")
        else:
            log.warning("\n⚠ QFC performance may need optimization")
            log.warning("Consider adjusting IM bias or mean photon number")

        log.info("\n" + "=" * 80)
        log.info("FULL QFC WORKFLOW COMPLETE")
        log.info("=" * 80)

        await self.__publish(
            {
                "full_workflow_complete": {
                    "pol_stabilized": self.pol_stabilized,
                    "hom_checked": self.hom_checked,
                    "bsm_checked": self.bsm_checked,
                    "qfc_initialized": self.qfc_initialized,
                    "hom_stab_visibility": hom_stab_results["visibility"],
                    "hom_qfc_visibility": float(hom_qfc_vis),
                    "bsm_stab_z_error": bsm_stab_results["error_rates"]["z_error"],
                    "bsm_stab_x_error": bsm_stab_results["error_rates"]["x_error"],
                    "bsm_qfc_z_error": bsm_qfc_results["error_rates"]["z_error"],
                    "bsm_qfc_x_error": bsm_qfc_results["error_rates"]["x_error"],
                    "qfc_performance_ok": qfc_performance_ok,
                    "ready_for_qfc": check_results["ready_for_qfc"],
                }
            }
        )

    def cancel(self):
        """Cancel the current workflow."""
        self.cancelled = True
        log.info("QFC workflow cancelled")

    async def __publish(self, payload):
        """Publish data via callback."""
        if self.cb is not None:
            await self.cb(payload)


# Legacy classes for compatibility
class Polarization:
    """Polarization parameters container."""

    def __init__(self, status=True, H_visibility=0.99, D_visibility=0.98, user_name=None):
        self.status = status
        self.H_visibility = H_visibility
        self.D_visibility = D_visibility
        self.user_name = user_name


class HOM:
    """HOM parameters container."""

    def __init__(self, visibility=0.48):
        self.visibility = visibility
        self.status = True


class BSM:
    """BSM parameters container."""

    def __init__(self, Z_error_rate=0.03, X_error_rate=0.28, status=True):
        self.Z_error_rate = Z_error_rate
        self.X_error_rate = X_error_rate
        self.status = status
