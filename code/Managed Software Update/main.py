#
#  main.py
#  Managed Software Update
#
#  Created by Greg Neagle on 2/10/10.
#  Copyright 2009-2010 Greg Neagle.
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


# import modules required by application

from PyObjCTools import AppHelper

# import modules containing classes required to start application and load MainMenu.nib

# pass control to AppKit
AppHelper.runEventLoop()
