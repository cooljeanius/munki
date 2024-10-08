#
#  MSUMainWindowController.py
#  Managed Software Update
#
#  Created by Greg Neagle on 2/11/10.
#  Copyright 2009-2011 Greg Neagle.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from objc import IBOutlet
from Foundation import *
from AppKit import *


class MSUMainWindowController(NSWindowController):
    """
    Controls the main window
    """

    theTabView = IBOutlet()
    theWindow = IBOutlet()

    def windowShouldClose_(self, sender):
        # just quit
        NSApp.terminate_(self)
