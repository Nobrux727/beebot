import json
from typing import TYPE_CHECKING

from beebot.actuator.actuator import ActuatorOutput
from beebot.sensor.sensor import SensoryOutput

if TYPE_CHECKING:
    from beebot.autosphere import Autosphere


def filter_list_directory_output(
    sphere: "Autosphere", sense: SensoryOutput, output: ActuatorOutput
):
    results = json.dumps(output.response.split("\n"))
    return f"The files in the directory are: {results}"