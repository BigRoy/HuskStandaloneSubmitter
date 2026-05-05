#!/usr/bin/env python3

from System import *
from System.Diagnostics import *
from System.IO import *

import re

from Deadline.Plugins import *
from Deadline.Scripting import *


def expand_frame_token(text, frame):
    """Expand $F (or with padding, e.g. $F4) to frame number

    This does not for strings with e.g. $FF or $FEND since
    it will assume the $F in that text is the frame token.

    Arguments:
        text (str): Text to expand.
        frame (int): Frame to format

    Returns:
        str: Text with $F expanded to given frame.

    """

    # TODO: Support all frame tokens as supported by husk --output flag, see:
    #       https://www.sidefx.com/docs/houdini/ref/utils/husk.html#rendersettings-overrides

    def replace_frame_token(match):
        number = match.group(2)
        if number:
            padding = int(number)
        else:
            padding = 1
        return str(frame).zfill(padding)

    return re.sub(r"(\$F([0-9]*))", replace_frame_token, text)


def GetDeadlinePlugin():
    return HuskStandalone()


def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()


class HuskStandalone(DeadlinePlugin):
    def __init__(self):
        super(HuskStandalone, self).__init__()
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable
        self.RenderArgumentCallback += self.RenderArgument
        self.IsSingleFramesOnlyCallback += self.SingleFrameOnly

    def Cleanup(self):
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback

    def InitializeProcess(self):
        self.StdoutHandling = True
        self.PopupHandling = False

        self.AddStdoutHandlerCallback(".*(\[driver.*\] .*can't create file .*\(No such file or directory\))").HandleCallback += self.HandleStdoutError  # capture error if output file can not be created
        self.AddStdoutHandlerCallback(".*ERROR.*\[texturesys\] .* (Could not open file .*)").HandleCallback += self.HandleStdoutError  # capture error if texture can't be loaded
        self.AddStdoutHandlerCallback("USD ERROR(.*)").HandleCallback += self.HandleStdoutError  # detect usd error
        self.AddStdoutHandlerCallback(r"ALF_PROGRESS ([0-9]+(?=%))").HandleCallback += self.HandleStdoutProgress
        self.AddStdoutHandlerCallback(".*OIIO Error: Error writing data to subimage(.*)").HandleCallback += self.HandleStdoutError  # detect OIIO error

    def RenderExecutable(self):
        """Return render executable path"""
        version = self.GetPluginInfoEntryWithDefault("Version", "")
        if version:
            version = "_" + version.replace(".", "_")
        return self.GetRenderExecutable("USD_RenderExecutable" + version)

    def RenderArgument(self):
        """Return arguments that go after the filename in the render command"""

        startFrame = self.GetStartFrame()
        endFrame = self.GetEndFrame()

        # construct filename
        usdFile = self.GetPluginInfoEntry("SceneFile")
        usdFile = RepositoryUtils.CheckPathMapping(usdFile)
        usdFile = usdFile.replace("\\", "/")

        # support frame token in input file paths (husk itself does not)
        usdFile = expand_frame_token(usdFile, self.GetStartFrame())

        self.LogInfo("Rendering USD file: " + usdFile)
        arguments = [usdFile]

        # frame arguments
        frameCount = endFrame - startFrame + 1
        arguments.append(f"--frame {startFrame}")
        arguments.append(f"--frame-count {frameCount}")

        # alfred style output and full verbosity
        arguments.append("--verbose")
        arguments.append("a{}".format(
            self.GetPluginInfoEntryWithDefault("LogLevel", ""))
        )

        # Allow plug-in info to override arguments to husk
        plugin_info_to_husk_arguments = {
            "Renderer": "renderer",
            "RenderSettings": "settings",
            "Purpose": "purpose",
            "Complexity": "complexity",
            "Snapshot": "snapshot",
            "PreRender": "prerender-script",
            "PreFrame": "preframe-script",
            "PostFrame": "postframe-script",
            "PostRender": "postrender-script",
        }
        for plugin_info_key, husk_flag in plugin_info_to_husk_arguments.items():
            value = self.GetPluginInfoEntryWithDefault(plugin_info_key, "")
            if value:
                arguments.append(f"--{husk_flag} {value}")

        # Tile rendering flags. Husk wants `--tile-count X Y` (two values)
        # plus `--tile-index N` and a printf-style `--tile-suffix` like
        # `_tile%02d` (husk substitutes the index itself). Only emitted when
        # TilesX > 0 and TilesY > 0 (i.e. this is actually a tile job).
        # TileIndex=0 is a real value (first tile), so we don't use the
        # falsy-check the generic loop above uses.
        try:
            tiles_x = int(
                self.GetPluginInfoEntryWithDefault("TilesX", "0")
            )
        except ValueError:
            tiles_x = 0
        try:
            tiles_y = int(
                self.GetPluginInfoEntryWithDefault("TilesY", "0")
            )
        except ValueError:
            tiles_y = 0
        if tiles_x > 0 and tiles_y > 0:
            try:
                tile_index = int(
                    self.GetPluginInfoEntryWithDefault("TileIndex", "-1")
                )
            except ValueError:
                tile_index = -1
            if tile_index >= 0:
                arguments.append(f"--tile-index {tile_index}")
                arguments.append(f"--tile-count {tiles_x} {tiles_y}")
                tile_suffix = self.GetPluginInfoEntryWithDefault(
                    "TileSuffix", ""
                )
                if tile_suffix:
                    arguments.append(f"--tile-suffix {tile_suffix}")

        # Default to restart delegate every frame since it's much more reliable
        # e.g. arnold just doesn't update per frame otherwise
        arguments.append("--restart-delegate 1")

        # If Houdini 20+ it may be that color space outputs are incorrect,
        # e.g. for Arnold. See: https://help.autodesk.com/view/ARNOL/ENU/?guid=arnold_for_houdini_solaris_ah_Solaris_FAQ_html   # noqa
        # They mention using the `--disable-dummy-raster-product` husk flag.

        arguments.append("--make-output-path")
        
        version = self.GetPluginInfoEntryWithDefault("Version", "")

        # We assume no version passed will be latest version, 
        # otherwise version would be in the form (major.minor) 
        # where we only consider the major version.
        if not version or float(version.split(".", 1)[0]) >= 20:
            # Supported only on Houdini 20+. 
            arguments.append("--disable-dummy-raster-product")

        # Slap Comp Args
        slapcomp_sources = self.GetPluginInfoEntryWithDefault("SlapCompSources", "")
        for source in slapcomp_sources.splitlines():
            if source:
                arguments.append(f"--slap-comp {source}")

        return " ".join(arguments)

    def SingleFrameOnly(self):
        """Return whether the task supports single frame only"""
        # Multi-frame rendering in a single `husk` call is supported, but only
        # if the input sequence is not a file per frame (anything with $F in
        # the filename) since it'd require each frame to load another file -
        # so if $F is present, we only support single frames per call.
        return "$F" in self.GetPluginInfoEntry("SceneFile")

    def HandleStdoutProgress(self):
        self.SetStatusMessage(self.GetRegexMatch(0))
        self.SetProgress(float(self.GetRegexMatch(1)))

    def HandleStdoutError(self):
        self.FailRender(self.GetRegexMatch(0))
