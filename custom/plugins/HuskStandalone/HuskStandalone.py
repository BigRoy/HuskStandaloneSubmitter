#!/usr/bin/env python3

from System import *
from System.Diagnostics import *
from System.IO import *

import os
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
        executable = self.GetRenderExecutable("USD_RenderExecutable" + version)

        if self.GetBooleanPluginInfoEntryWithDefault("Stitch", False):
            # Houdini ships itilestitch next to husk
            extension = os.path.splitext(executable)[1]  # .exe on Windows
            executable = os.path.join(
                os.path.dirname(executable), "itilestitch" + extension
            )
            self.LogInfo("Stitch executable: " + executable)

        return executable

    def RenderArgument(self):
        """Return arguments that go after the filename in the render command"""
        if self.GetBooleanPluginInfoEntryWithDefault("Stitch", False):
            return self.StitchArgument()

        startFrame = self.GetStartFrame()
        endFrame = self.GetEndFrame()
        if self.IsTileJob():
            # A Deadline tile job renders one tile of a single frame per task
            endFrame = startFrame

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
            "RenderPass": "pass",
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

        # Tile rendering
        tile_index = self.GetIntegerPluginInfoEntryWithDefault("TileIndex", -1)
        if self.IsTileJob():
            # Each task of a Deadline tile job renders the tile of its task id
            tile_index = int(self.GetCurrentTaskId())

        if self.GetBooleanPluginInfoEntryWithDefault("AutoTile", False):
            # Husk renders all tiles and stitches them in a single process
            tiles_x, tiles_y = self.GetTileCount()
            arguments.append("--autotile")
            arguments.append(f"--tile-count {tiles_x} {tiles_y}")
            auto_tile_res = self.GetPluginInfoEntryWithDefault(
                "AutoTileRes", ""
            )
            if auto_tile_res:
                arguments.append(f"--autotile-res {auto_tile_res}")
        elif tile_index >= 0:
            # Render a single tile, a stitch job assembles the final image
            tiles_x, tiles_y = self.GetTileCount()
            arguments.append(f"--tile-index {tile_index}")
            arguments.append(f"--tile-count {tiles_x} {tiles_y}")
            arguments.append(f"--tile-suffix {self.GetTileSuffix()}")

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

    def StitchArgument(self):
        """Return itilestitch arguments: the output image, then its tiles"""
        frame = self.GetStartFrame()
        if self.GetEndFrame() != frame:
            self.FailRender(
                "Stitching supports a single frame per task. "
                "Set the job's frames per task to 1."
            )

        # The output image to stitch is the stitch job's only output
        job = self.GetJob()
        outputs = [
            os.path.join(directory, filename) for directory, filename
            in zip(job.JobOutputDirectories, job.JobOutputFileNames)
        ]
        if len(outputs) != 1:
            self.FailRender(
                f"Stitching requires a single job output, got {len(outputs)}."
            )
        output = RepositoryUtils.CheckPathMapping(outputs[0])
        output = output.replace("\\", "/")
        output = FrameUtils.ReplacePaddingWithFrameNumber(output, frame)

        # Husk writes each tile next to the output image, with the tile suffix
        # before the extension, and expands $F or %d in it to the tile index.
        tiles_x, tiles_y = self.GetTileCount()
        tile_suffix = self.GetTileSuffix()
        root, ext = os.path.splitext(output)
        tiles = []
        for tile_index in range(tiles_x * tiles_y):
            suffix = expand_frame_token(tile_suffix, tile_index)
            suffix = re.sub(
                r"%\d*d", lambda match: match.group(0) % tile_index, suffix
            )
            tiles.append(root + suffix + ext)

        # Without the tile index in the suffix all tiles wrote the same file,
        # and itilestitch would stitch that single tile without an error.
        if len(set(tiles)) != len(tiles):
            self.FailRender(
                f"TileSuffix '{tile_suffix}' has no tile index ($F or %d), "
                "so all tiles were written to the same file."
            )

        # itilestitch does not say which tile is missing
        missing = [tile for tile in tiles if not os.path.isfile(tile)]
        if missing:
            self.FailRender("Missing tiles:\n" + "\n".join(missing))

        self.LogInfo(f"Stitching {len(tiles)} tiles into: {output}")
        return " ".join([output] + tiles)

    def GetTileCount(self):
        """Return the number of tiles in x and y"""
        tiles_x = self.GetIntegerPluginInfoEntryWithDefault("TilesX", 0)
        tiles_y = self.GetIntegerPluginInfoEntryWithDefault("TilesY", 0)
        if tiles_x < 1 or tiles_y < 1:
            self.FailRender(
                "Tile rendering requires TilesX and TilesY, "
                f"got {tiles_x}x{tiles_y}."
            )
        return tiles_x, tiles_y

    def GetTileSuffix(self):
        """Return the suffix husk adds to each tile's output name"""
        tile_suffix = self.GetPluginInfoEntryWithDefault("TileSuffix", "")
        if not tile_suffix:
            # Otherwise all tiles would write to the same file
            self.FailRender(
                "Tile rendering requires TileSuffix, e.g. _tile%02d"
            )
        return tile_suffix

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
