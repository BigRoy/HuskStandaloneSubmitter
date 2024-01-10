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
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable  # get the renderExecutable Location
        self.RenderArgumentCallback += self.RenderArgument      # get the arguments to go after the EXE
        self.IsSingleFramesOnlyCallback += self.SingleFrameOnly      # get the arguments to go after the EXE

    def Cleanup(self):
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback

    def InitializeProcess(self):
        self.StdoutHandling = True
        self.PopupHandling = False

        self.AddStdoutHandlerCallback("USD ERROR(.*)").HandleCallback += self.HandleStdoutError # detect this error
        self.AddStdoutHandlerCallback(r"ALF_PROGRESS ([0-9]+(?=%))").HandleCallback += self.HandleStdoutProgress

    def RenderExecutable(self):
        """Return render executable path"""
        return self.GetRenderExecutable("USD_RenderExecutable")

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
        arguments.append("a{}".format(self.GetPluginInfoEntryWithDefault("LogLevel", "")))
        
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
                
        # Default to restart delegate every frame since it's much more reliable
        # e.g. arnold just doesn't update per frame otherwise
        arguments.append("--restart-delegate 1")
        
        # If Houdini 20+ it may be that color space outputs are incorrect, e.g. for Arnold.
        # See: https://help.autodesk.com/view/ARNOL/ENU/?guid=arnold_for_houdini_solaris_ah_Solaris_FAQ_html
        # They mention using the new `--disable-dummy-raster-product` husk flag.
        
        arguments.append("--make-output-path")
        return " ".join(arguments)
        
    def SingleFrameOnly(self):
        """Return whether the task supports single frame only"""
        # Multi-frame rendering in a single `husk` call is supported, but only if the input sequence
        # is not a file per frame (anything with $F in the filename) since it'd require each frame to
        # load another file - so if $F is present, we only support single frames per call.
        return "$F" in self.GetPluginInfoEntry("SceneFile")

    def HandleStdoutProgress(self):
        self.SetStatusMessage(self.GetRegexMatch(0))
        self.SetProgress(float(self.GetRegexMatch(1)))

    def HandleStdoutError(self):
        self.FailRender(self.GetRegexMatch(0))
