#
#  MSUupdatesViewController.py
#  Managed Software Update
#
#  Created by Greg Neagle on 7/8/10.
#  Copyright 2010-2011 Greg Neagle.
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

from objc import YES, NO, IBAction, IBOutlet
import munki
from Foundation import *
from AppKit import *

munki.setupLogging()


class MSUupdatesViewController(NSViewController):
    """
    Controls the updates view of the main window
    """

    restartInfoFld = IBOutlet()
    restartImageFld = IBOutlet()
    descriptionView = IBOutlet()
    tableView = IBOutlet()
    optionalSoftwareBtn = IBOutlet()
    array_controller = IBOutlet()
    window_controller = IBOutlet()
    updateNowBtn = IBOutlet()

    _EMPTYUPDATELIST = NSArray.arrayWithArray_(
        [
            {
                "image": NSImage.imageNamed_("Empty.png"),
                "name": "",
                "version": "",
                "size": "",
                "description": "",
            }
        ]
    )
    _updatelist = []

    def updatelist(self):
        # NSLog(u"MSUupdatesViewController.updatelist")
        return self._updatelist or self._EMPTYUPDATELIST

    objc.accessor(updatelist)  # PyObjC KVO hack

    def setUpdatelist_(self, newlist):
        # NSLog(u"MSUupdatesViewController.setUpdatelist_")
        self._updatelist = NSArray.arrayWithArray_(newlist)

    objc.accessor(setUpdatelist_)  # PyObjC KVO hack

    @IBAction
    def laterBtnClicked_(self, sender):
        NSApp.delegate().laterBtnClicked()

    @IBAction
    def updateNowBtnClicked_(self, sender):
        # alert the user to logout, proceed without logout, or cancel
        NSApp.delegate().confirmInstallUpdates()

    @IBAction
    def optionalSoftwareBtnClicked_(self, sender):
        # switch to optional software pane
        munki.log("user", "view_optional_software")
        self.window_controller.theTabView.selectNextTabViewItem_(sender)
        NSApp.delegate().optional_view_controller.AddRemoveBtn.setEnabled_(NO)
        # NSApp.delegate().buildOptionalInstallsData()

    def updateWebKitView_(self, description):
        if "</html>" in description or "</HTML>" in description:
            self.descriptionView.mainFrame().loadHTMLString_baseURL_(description, None)
        else:
            self.descriptionView.mainFrame().loadData_MIMEType_textEncodingName_baseURL_(
                buffer(description.encode("UTF-8")), "text/plain", "utf-8", None
            )

    def updateDescriptionView(self):
        # NSLog(u"MSUupdatesViewController.updateDescriptionView")
        if len(self.array_controller.selectedObjects()):
            row = self.array_controller.selectedObjects()[0]
            description = row.get("description", "")
        else:
            description = ""
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            self.updateWebKitView_, description, YES
        )

    def tableViewSelectionDidChange_(self, sender):
        # NSLog(u"MSUupdatesViewController.tableViewSelectionDidChange_")
        # self.performSelectorOnMainThread_withObject_waitUntilDone_(self.updateDescriptionView, None, NO)
        self.updateDescriptionView()
