from typing import List, Optional
from linkCal.interpreter.PSO.RigolManager_for_QFC import ChannelParams
from linkCal.interpreter.PSO.utility import MeasurementType
import numpy as np
import asyncio


class Attenuation_Manager:
    """
    Manager for controlling an attenuation channel via a Rigol function generator.
    Mirrors the structure of IM_Manager, but without RF handling.
    """

    def __init__(self, dc_src=None, dc_params=None):
        self.dc_src = dc_src  # Can be HAL device or legacy RigolManager
        self.dc_params: Optional[ChannelParams] = dc_params
        self.default_enable_output: bool = True
        self.attn_optimized: bool = False
        self.attn_optimized_v: Optional[float] = 4.8
        self.attn_optimized_counts: Optional[np.ndarray] = 0
        self.max_voltage: float = 4.9  # Max voltage limit for attenuation control

    # -------------------- set_dc --------------------
    async def set_dc(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        *,
        enable_output: Optional[bool] = None,
        level_v: Optional[float] = None,
        printout: bool = True,
    ) -> float:
        """
        Configure the given channel to DC mode and set its level.
        """

        # Fallback logic
        src = dc_src or self.dc_src
        if src is None:
            raise ValueError("dc_src must be provided either as argument or in Attn_Manager.dc_src.")

        chp = ch_params or self.dc_params
        if chp is None:
            raise ValueError("ch_params must be provided either as argument or in Attn_Manager.dc_params.")

        ch = int(chp.channel)
        if ch not in (1, 2):
            raise ValueError("Channel must be 1 or 2.")

        # Determine voltage to use
        if level_v is not None:
            dc_level = float(level_v)
        elif getattr(chp, "dc_level_v", None) is not None:
            dc_level = float(chp.dc_level_v)
        elif getattr(chp, "offset_v", None) is not None:
            dc_level = float(chp.offset_v)
        else:
            dc_level = 0.0

        params = ChannelParams(channel=ch, mode="dc", dc_level_v=dc_level)
        eo = self.default_enable_output if (enable_output is None) else bool(enable_output)

        # Use HAL device async methods if available
        if hasattr(src, "configure"):
            # HAL device
            await src.configure(channel=ch, mode="dc", dc_level_v=dc_level)
            if eo:
                await src.set(channel=ch, setting="ON")
            else:
                await src.set(channel=ch, setting="OFF")
        else:
            # Legacy RigolManager
            src.set_channel(params, enable_output=eo)

        if printout:
            print(f"[Attn_Manager] CH{ch} set to DC mode: {dc_level:.3f} V")

        return dc_level

    async def set_max_attn(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        *,
        enable_output: Optional[bool] = None,
        level_v: Optional[float] = None,
        printout: bool = True,
    ) -> float:
        """
        Configure the given channel to DC mode and set its level.
        """

        # Fallback logic
        src = dc_src or self.dc_src
        if src is None:
            raise ValueError("dc_src must be provided either as argument or in Attn_Manager.dc_src.")

        chp = ch_params or self.dc_params
        if chp is None:
            raise ValueError("ch_params must be provided either as argument or in Attn_Manager.dc_params.")

        ch = int(chp.channel)
        if ch not in (1, 2):
            raise ValueError("Channel must be 1 or 2.")

        # Determine voltage to use
        if level_v is not None:
            dc_level = float(level_v)
        else:
            dc_level = self.max_voltage

        params = ChannelParams(channel=ch, mode="dc", dc_level_v=dc_level)
        eo = self.default_enable_output if (enable_output is None) else bool(enable_output)

        # Use HAL device async methods if available
        if hasattr(src, "configure"):
            # HAL device
            await src.configure(channel=ch, mode="dc", dc_level_v=dc_level)
            if eo:
                await src.set(channel=ch, setting="ON")
            else:
                await src.set(channel=ch, setting="OFF")
        else:
            # Legacy RigolManager
            src.set_channel(params, enable_output=eo)
        if printout:
            print(f"[Attn_Manager] CH{ch} set to DC mode: {dc_level:.3f} V to get max attenuation")
        return dc_level

    # -------------------- set_dc_and_measure --------------------
    async def set_dc_and_measure(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        timetagger=None,
        *,
        level_v: Optional[float] = None,
        Chlist: Optional[List[int]] = None,
        measurement_time: float = 0.5,
        printout: bool = True,
        settle_time: float = 0.2,
    ) -> np.ndarray:
        """
        Set DC bias and measure count rates from the TimeTagger.
        """
        src = dc_src or self.dc_src
        if src is None:
            raise ValueError("dc_src must be provided either as argument or in Attn_Manager.dc_src.")
        if timetagger is None:
            raise ValueError("timetagger must be provided as a TimeTaggerManager instance.")

        # Apply bias
        dc_level = await self.set_dc(
            dc_src=src, ch_params=ch_params, enable_output=True, level_v=level_v, printout=printout
        )
        await asyncio.sleep(settle_time)

        channels = Chlist if Chlist is not None else getattr(timetagger, "Chlist", Chlist)
        if not channels:
            raise ValueError("No channels to measure: provide Chlist or ensure timetagger.Chlist is set.")

        # Use HAL device async methods if available
        if hasattr(timetagger, "measure"):
            # HAL device
            count_rates = await timetagger.measure(
                MeasurementType.RATE, channels=channels, measurement_time=measurement_time, printout=printout
            )
        else:
            # Legacy TimeTaggerManager
            count_rates = timetagger.getChannelCountRate(channels, measurement_time, printout=printout)
        if printout:
            print(f"[Attn_Manager] Measured count rates after setting {dc_level:.3f} V:")
            print(f"  Channels: {channels}")
            print(f"  Rates (/s): {count_rates}")

        return np.array(count_rates, dtype=float)

    # -------------------- optimize_attenuation --------------------
    async def optimize_attenuation(
        self,
        dc_src=None,
        ch_params: ChannelParams = None,
        timetagger=None,
        *,
        Chlist: Optional[List[int]] = None,
        vmin: float = -5.0,
        vmax: float = 7.0,
        init_v: Optional[float] = None,
        step: float = 0.1,
        measurement_time: float = 0.1,
        target: float = 1e5,
        tolerance: float = 0.02,
        max_iters: int = 40,
        printout: bool = True,
        settle_time: float = 0.1,
        grad_floor: float = 1e3,
        lr_clip: float = 0.5,
    ) -> float:
        """
        Optimize attenuation voltage to reach target count rate.
        """
        self.attn_optimized = False

        src = dc_src or self.dc_src
        chp = ch_params or self.dc_params
        if src is None:
            raise ValueError("dc_src must be provided either as argument or in Attn_Manager.dc_src.")
        if timetagger is None:
            raise ValueError("timetagger must be provided as a TimeTaggerManager instance.")
        if chp is None:
            raise ValueError("ch_params must be provided either as argument or in Attn_Manager.dc_params.")

        channels = Chlist if Chlist is not None else getattr(timetagger, "Chlist", Chlist)
        v = float(init_v) if init_v is not None else 0.5 * (vmin + vmax)
        best_err, best_v = np.inf, v

        if printout:
            print(
                f"[Attn_Manager] Starting attenuation optimization: target={target:.3e} cps | "
                f"init V={v:.3f} in [{vmin:.2f}, {vmax:.2f}]"
            )

        async def measure_counts(volts: float) -> float:
            return float(
                np.sum(
                    await self.set_dc_and_measure(
                        src,
                        chp,
                        timetagger,
                        level_v=volts,
                        Chlist=channels,
                        measurement_time=measurement_time,
                        printout=False,
                        settle_time=settle_time,
                    )
                )
            )

        for i in range(1, max_iters + 1):
            rate_v = await measure_counts(v)
            v_minus = max(vmin, v - step)
            v_plus = min(vmax, v + step)

            rate_minus = await measure_counts(v_minus)
            rate_plus = await measure_counts(v_plus)

            grad = (rate_plus - rate_minus) / max(v_plus - v_minus, 1e-9)
            if abs(grad) < grad_floor:
                grad = np.sign(grad) * grad_floor if grad != 0 else -grad_floor

            delta_v = -(rate_v - target) / grad
            delta_v = float(np.clip(delta_v, -lr_clip, lr_clip))
            v_new = float(np.clip(v + delta_v, vmin, vmax))

            err = abs(rate_v - target) / max(target, 1)
            print(
                f"Iter {i:02d}: V={v:.4f} rate={rate_v:.2e} grad={grad:.2e} "
                f"dV={delta_v:+.4f} rel_err={err:.3f} -> next V={v_new:.4f}"
            )

            if err < best_err:
                best_err, best_v = err, v

            if err <= tolerance:
                if printout:
                    print(f"[Attn_Manager] Reached target: rel_err={err:.3f} at V={v:.4f} (rate={rate_v:.2e})")
                self.attn_optimized = True
                self.attn_optimized_v = best_v
                self.attn_optimized_counts = rate_v
                return v

            if abs(v_new - v) < 1e-3:
                if printout:
                    print(f"[Attn_Manager] Small ΔV stop at V={v_new:.4f} (rate={rate_v:.2e})")
                break

            v = v_new

        if printout:
            print(f"[Attn_Manager] Max iterations reached. Best V={best_v:.4f} (rel_err={best_err:.3f})")
        return best_v
