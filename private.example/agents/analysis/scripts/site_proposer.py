"""Copy into private/ and return user-approved design points.

This is an interface sketch, not an optimizer or a PDK-specific g_m/I_D rule.
The compact context contains campaign policy and the prior comparison, never
raw waveforms. The returned proposal is not submitted until reviewed and added.
"""


def propose(context):
    raise NotImplementedError("Choose legal initial/next points using your private expertise")
