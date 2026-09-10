#!/bin/env python





from __future__ import print_function
from builtins import str

import unittest
import time

from WMCore.Services.UUIDLib import makeUUID

class UUIDTest(unittest.TestCase):


    def setUp(self):
        pass

    def tearDown(self):
        pass


    def testUUID(self):

        listOfIDs = []

        refID = makeUUID()

        for i in range(0,1000):
            tmpID = makeUUID()
            # Compare whole UUIDs, never single components: the second
            # component is 16 random bits, so one of 1000 fresh UUIDs matched
            # the reference about once in 66 test runs and failed the suite.
            self.assertNotEqual(tmpID, refID, "UUID identical to the reference: %s" % tmpID)
            self.assertEqual(type(tmpID), str)
            self.assertEqual(listOfIDs.count(tmpID), 0, "UUID repeated!  %s found in list %i times!"
                             % (tmpID, listOfIDs.count(tmpID)))
            listOfIDs.append(tmpID)



        return


    def testTime(self):

        nUIDs     = 100000
        startTime = time.time()
        for i in range(0,nUIDs):
            makeUUID()
        print("We can make %i UUIDs in %f seconds" %(nUIDs, time.time() - startTime))

if __name__ == '__main__':
    unittest.main()
