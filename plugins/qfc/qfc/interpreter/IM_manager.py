from typing import List, Optional
from linkCal.interpreter.PSO.RigolManager_for_QFC import ChannelParams
from linkCal.interpreter.PSO.utility import MeasurementType
import numpy as np
import asyncio


class IM_Manager:
    """
    Manager for IM bias control using a Rigol function generator.
    """

    def __init__(self, dc_src=None, dc_params=None, rf_src=None, rf_params=None):
        self.dc_src = dc_src  # Can be HAL device or legacy RigolManager
        self.rf_src = rf_src  # Can be HAL device or legacy RigolManager
        self.dc_params: Optional[ChannelParams] = dc_params
        self.rf_params: Optional[ChannelParams] = rf_params
        self.default_enable_output: bool = True
        self.rf_status: bool = False  # Track RF output status
        self.dc_optimized: bool = False  # Track DC optimization status
        self.dc_optimized_v: Optional[float] = 2.5  # Store optimized DC voltage

    async def set_dc(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        *,
        enable_output: Optional[bool] = None,
        level_v: Optional[float] = None,
        printout: bool = False,
    ) -> float:
        """
        Configure the given channel to DC mode and set its level.

        Parameters
        ----------
        rigol : RigolDG4162Manager
            Connected Rigol manager instance.
        ch_params : ChannelParams
            Must include `channel` (1 or 2). If `dc_level_v` is not provided,
            uses `offset_v` or the explicit `level_v` argument.
        enable_output : bool, optional
            Whether to enable output after configuration. If None, uses
            `self.default_enable_output`.
        level_v : float, optional
            Override voltage level (V). If given, replaces any value in
            `ch_params.dc_level_v`.

        Returns
        -------
        float
            The DC level (in volts) that was set.
        """

        if dc_src is None:
            if self.dc_src is not None:
                dc_src = self.dc_src
            else:
                raise ValueError("dc_src must be provided either as argument or in IM_Manager.dc_src.")

        if ch_params is None:
            if self.dc_params is not None:
                ch_params = self.dc_params
            else:
                raise ValueError("ch_params must be provided either as argument or in IM_Manager.dc_params.")

        ch = int(ch_params.channel)
        if ch not in (1, 2):
            raise ValueError("Channel must be 1 or 2.")

        # Decide what voltage to use
        if level_v is not None:
            dc_level = float(level_v)
        elif ch_params.dc_level_v is not None:
            dc_level = float(ch_params.dc_level_v)
        elif ch_params.offset_v is not None:
            dc_level = float(ch_params.offset_v)
        else:
            dc_level = 0.0

        # Build new ChannelParams for DC mode
        params = ChannelParams(channel=ch, mode="dc", dc_level_v=dc_level)

        # Determine output enable state
        eo = self.default_enable_output if (enable_output is None) else bool(enable_output)

        # Use HAL device async methods if available
        if hasattr(dc_src, "configure"):
            # HAL device
            await dc_src.configure(channel=ch, mode="dc", dc_level_v=dc_level)
            if eo:
                await dc_src.set(channel=ch, setting="ON")
            else:
                await dc_src.set(channel=ch, setting="OFF")
        else:
            # Legacy RigolManager
            dc_src.set_channel(params, enable_output=eo)

        if printout:
            print(f"[IM_Manager] CH{ch} set to DC mode: {dc_level:.3f} V")
        return dc_level

    # -------------------- set_dc_and_measure --------------------
    async def set_dc_and_measure(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        timetagger=None,
        level_v: Optional[float] = None,
        Chlist: Optional[List[int]] = None,
        measurement_time: float = 0.5,
        printout: bool = True,
    ) -> np.ndarray:
        """
        Sets a DC bias on the Rigol and immediately measures count rates
        on given TimeTagger channels.

        Parameters
        ----------
        rigol : RigolDG4162Manager
            Connected Rigol device.
        ch_params : ChannelParams
            Contains channel number and DC bias voltage.
        timetagger : TimeTaggerManager
            Initialized TimeTaggerManager instance.
        Chlist : list of int, optional
            Channels to measure. Defaults to all channels in config.
        measurement_time : float, default 1.0
            Duration in seconds for the count-rate measurement.
        printout : bool, default True
            Whether to print measured values.

        Returns
        -------
        np.ndarray
            Array of measured count rates (/s) for the specified channels.
        """
        # Step 1: Set DC bias
        if level_v is not None:
            dc_level = await self.set_dc(
                dc_src=dc_src, ch_params=ch_params, enable_output=True, level_v=level_v, printout=printout
            )
        else:
            dc_level = await self.set_dc(dc_src=dc_src, ch_params=ch_params, enable_output=True, printout=printout)

        if timetagger is None:
            raise ValueError("timetagger must be provided as a TimeTaggerManager instance.")

        await asyncio.sleep(0.2)
        # Step 2: Determine channel list
        channels = Chlist if Chlist is not None else getattr(timetagger, "Chlist", Chlist)

        # Step 3: Measure count rates using TimeTagger
        if hasattr(timetagger, "measure"):
            # HAL device
            count_rates = await timetagger.measure(
                MeasurementType.RATE, channels=channels, measurement_time=measurement_time, printout=printout
            )
        else:
            # Legacy TimeTaggerManager
            count_rates = timetagger.getChannelCountRate(channels, measurement_time, printout=printout)

        if printout:
            print(f"[IM_Manager] Measured count rates after setting {dc_level:.3f} V DC:")
            print(f"Channels: {channels}")
            print(f"Rates (/s): {count_rates}")

        return np.array(count_rates)

    # -------------------- set_RF --------------------
    async def set_RF(
        self,
        rf_src=None,
        ch_params: ChannelParams = None,
        enable_output: Optional[bool] = None,
    ):
        """
        Configure the RF drive channel using RigolManager.set_channel().

        Parameters
        ----------
        rigol : RigolDG4162Manager
            Connected Rigol manager instance.
        ch_params : ChannelParams
            RF configuration parameters (mode = 'square', 'burst_sine', or 'dc').
        enable_output : bool, optional
            Whether to enable output after configuration. If None, uses default.

        Notes
        -----
        This is a simple wrapper around rigol.set_channel().
        All parameter validation and SCPI handling are performed inside RigolManager.
        """

        if rf_src is None:
            if self.rf_src is not None:
                rf_src = self.rf_src
            else:
                raise ValueError("rf_src must be provided either as argument or in IM_Manager.rf_src.")

        if ch_params is None:
            if self.rf_params is not None:
                ch_params = self.rf_params
            else:
                raise ValueError("ch_params must be a ChannelParams instance (not None).")

        eo = self.default_enable_output if (enable_output is None) else bool(enable_output)

        # Use HAL device async methods if available
        if hasattr(rf_src, "configure"):
            # HAL device - configure RF parameters
            ch = int(ch_params.channel)
            config_params = {
                "channel": ch,
                "mode": ch_params.mode,
            }
            if ch_params.mode == "square":
                config_params.update(
                    {
                        "square_freq_hz": ch_params.square_freq_hz,
                        "amplitude_vpp": ch_params.amplitude_vpp,
                        "offset_v": ch_params.offset_v or 0.0,
                        "duty_percent": ch_params.duty_percent or 50.0,
                        "phase_deg": ch_params.phase_deg or 0.0,
                    }
                )
            await rf_src.configure(**config_params)
            if eo:
                await rf_src.set(channel=ch, setting="ON")
            else:
                await rf_src.set(channel=ch, setting="OFF")
        else:
            # Legacy RigolManager
            rf_src.set_channel(ch_params, enable_output=eo)

        print(
            f"[IM_Manager] CH{ch_params.channel} RF configured with mode '{ch_params.mode}' "
            f"and output {'ENABLED' if eo else 'DISABLED'}"
        )
        self.rf_status = eo

    # -------------------- optimize_dc_bias (UPDATED) --------------------
    async def optimize_dc_bias(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        timetagger=None,
        *,
        Chlist: Optional[List[int]] = None,
        vmin: float = -7.0,
        vmax: float = 7.0,
        init_v: Optional[float] = None,
        step: float = 0.05,
        measurement_time: float = 0.2,
        # --- NEW: target + error tolerance ---
        target_value: Optional[float] = None,
        tolerance: Optional[float] = 0.05,  # compare ABS(ERROR) to this when target_value is provided
        # -------------------------------------
        max_iters: int = 40,
        printout: bool = True,
        settle_time: float = 0.1,
    ) -> float:
        """
        Optimize the DC bias voltage.

        If `target_value` is None:
            Minimize total count rate across `Chlist`.

        If `target_value` is provided:
            Minimize absolute error |counts - target_value| and STOP when
            |counts - target_value| <= tolerance (default 0.02).
        """
        self.dc_optimized = False
        self.temp_rf_status = self.rf_status if hasattr(self, "rf_status") else False

        if self.rf_status:
            print("[IM_Manager] Disabling RF output for DC bias optimization.")
            await self.set_RF(rf_src=self.rf_src, ch_params=self.rf_params, enable_output=False)
            print("[IM_Manager] RF output disabled.")

        if timetagger is None:
            raise ValueError("timetagger must be provided as a TimeTaggerManager instance.")

        channels = Chlist if Chlist is not None else getattr(timetagger, "Chlist", Chlist)
        v = init_v if init_v is not None else (vmin + vmax) / 2.0

        async def measure_counts(volts: float) -> float:
            total = np.sum(
                await self.set_dc_and_measure(
                    dc_src,
                    ch_params,
                    timetagger,
                    level_v=volts,
                    Chlist=channels,
                    measurement_time=measurement_time,
                    printout=False,
                )
            )
            return float(total)

        def objective(counts: float) -> float:
            # If target_value is set, the objective is ABS error to the target;
            # else objective is just the counts (minimize counts).
            return abs(counts - target_value) / target_value if (target_value is not None) else counts

        best_v = float(v)
        counts_v = await measure_counts(v)
        obj_v = objective(counts_v)
        best_obj = obj_v
        best_counts = counts_v

        prev_counts = counts_v

        if printout:
            if target_value is None:
                print(f"\n[IM_Manager] DC bias optimization (min counts): init V={v:.3f}, counts={counts_v:.6g}")
                print("Iter |     V (V) |   counts      |  |Δcounts|/counts |    obj        |    grad")
                print("-----+-----------+---------------+--------------------+---------------+----------------")
            else:
                err0 = abs(counts_v - target_value) / target_value
                print(
                    f"\n[IM_Manager] DC bias optimization (target={target_value:.6g}, tol={tolerance:.6g}): "
                    f"init V={v:.3f}, counts={counts_v:.6g}, |err|={err0:.6g}"
                )
                print("Iter |     V (V) |   counts      |   |err|          |    obj        |    grad")
                print("-----+-----------+---------------+------------------+---------------+----------------")

        for i in range(1, max_iters + 1):
            # Evaluate at v±step (bounded)
            v_minus = max(vmin, v - step)
            v_plus = min(vmax, v + step)

            counts_minus = await measure_counts(v_minus)
            await asyncio.sleep(settle_time)
            counts_plus = await measure_counts(v_plus)
            await asyncio.sleep(settle_time)

            obj_minus = objective(counts_minus)
            obj_plus = objective(counts_plus)

            denom = v_plus - v_minus
            grad = (obj_plus - obj_minus) / denom if denom > 0 else 0.0

            # Decreasing aggressiveness as iterations increase
            # (keeps your flavor but targets the objective move)
            k = max(1.0, (20 - (i - 1)) / 5.0)  # 4.0→…→1.0 over first ~20 iters
            if grad == 0.0:
                # Flat region: nudge toward the side with smaller objective
                direction = -1.0 if obj_plus > obj_minus else 1.0
            else:
                direction = -np.sign(grad)

            v_new = float(np.clip(v + k * direction * step, vmin, vmax))
            counts_new = await measure_counts(v_new)
            obj_new = objective(counts_new)

            if target_value is None:
                conv_metric = abs(counts_new - prev_counts) / max(1.0, prev_counts)
                log_delta = f"Δcounts={counts_new - prev_counts:.3e} ({conv_metric:>.3e} rel.)"
            else:
                rel_err = abs(counts_new - target_value) / target_value
                log_delta = f"|err|={abs(counts_new - target_value):.6g} ({rel_err:.3e} rel.)"

            if printout:
                print(f"{i:4d} | {v_new:10.4f} | {counts_new:>13.6g} | {log_delta} | {obj_new:>13.6g} | {grad:>14.3e}")

            # Track best
            if obj_new < best_obj:
                best_obj, best_v, best_counts = obj_new, v_new, counts_new

            # Convergence (only meaningful in target-tracking mode)
            if (target_value is not None) and (obj_new <= tolerance):
                if printout:
                    print(
                        f"[IM_Manager] Reached tolerance: V={v_new:.4f}, counts={counts_new:.6g}, "
                        f"|err|={obj_new:.6g} ≤ {tolerance}"
                    )
                self.dc_optimized = True
                best_v, best_counts = v_new, counts_new
                break

            if target_value is None:
                if conv_metric <= tolerance and abs(prev_counts - counts_new) <= 500:
                    self.dc_optimized = True
                    break
            else:
                if rel_err <= tolerance:
                    self.dc_optimized = True
                    best_v, best_counts = v_new, counts_new
                    break

            prev_counts = counts_new
            # Move center
            v, counts_v, obj_v = v_new, counts_new, obj_new

        # Leave channel at best found
        await self.set_dc(dc_src, ch_params, enable_output=True, level_v=best_v)
        if target_value is None:
            print(
                f"[IM_Manager] Final optimized bias (min counts): V={best_v:.3f}  "
                f"counts={best_counts:.6g}  obj={best_obj:.6g}"
            )
        else:
            print(
                f"[IM_Manager] Final optimized bias (target): V={best_v:.3f}  "
                f"counts={best_counts:.6g}  |err|={best_obj:.6g}  tol={tolerance}"
            )

        if self.temp_rf_status:
            print("[IM_Manager] Restoring RF output after DC bias optimization.")
            await self.set_RF(rf_src=self.rf_src, ch_params=self.rf_params, enable_output=True)
            print("[IM_Manager] RF output restored.")

        self.dc_optimized_v = best_v

        return best_v
