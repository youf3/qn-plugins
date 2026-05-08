from typing import Optional


class ChannelParams:
    """
    Data class for Rigol channel parameters.
    Used for sharing configuration between QFC and BSM experiments.
    """

    def __init__(
        self,
        channel: int,
        mode: Optional[str] = None,
        dc_level_v: Optional[float] = None,
        offset_v: Optional[float] = None,
        square_freq_hz: Optional[float] = None,
        amplitude_vpp: Optional[float] = None,
        duty_percent: Optional[float] = None,
        phase_deg: Optional[float] = None,
        **kwargs,
    ):
        self.channel = int(channel)
        self.mode = mode
        self.dc_level_v = dc_level_v
        self.offset_v = offset_v
        self.square_freq_hz = square_freq_hz
        self.amplitude_vpp = amplitude_vpp
        self.duty_percent = duty_percent
        self.phase_deg = phase_deg
        # Store any other params
        for key, value in kwargs.items():
            setattr(self, key, value)


class RigolDG4162Manager:
    """
    Mock/Legacy Rigol manager for QFC specific usage.
    """

    def __init__(self, resource):
        self.resource = resource

    def connect(self):
        pass

    def disconnect(self):
        pass

    def set_channel(self, params: ChannelParams, enable_output: bool = True):
        # Implementation depends on the physical device or HAL wrapper
        pass
