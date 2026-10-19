"""Launch background tools without exposing console windows on Windows."""
import os
import subprocess


def background_options(new_session=False):
    if os.name=='nt':
        startup=subprocess.STARTUPINFO()
        startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=subprocess.SW_HIDE
        # Keep a hidden console for npm/Git descendants to inherit. A windowless
        # parent alone does not prevent those programs allocating a new console.
        return {'creationflags':subprocess.CREATE_NEW_CONSOLE,'startupinfo':startup}
    return {'start_new_session':True} if new_session else {}
