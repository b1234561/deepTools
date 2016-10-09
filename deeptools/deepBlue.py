#!/usr/bin/env python
try:
    # python 2
    import xmlrpclib
except:
    # python 3
    import xmlrpc.client as xmlrpclib
import time
import numpy as np
import tempfile
import pyBigWig
from deeptools.utilities import mungeChromosome
from deeptoolsintervals import GTF


def isDeepBlue(fname):
    """
    Returns true if the file ends in .wig, .wiggle, or .bedgraph, since these indicate a file on the deepBlue server
    """
    if fname.endswith(".wig"):
        return True
    if fname.endswith(".wiggle"):
        return True
    if fname.endswith(".bedgraph"):
        return True
    return False


def mergeRegions(regions):
    """
    Given a list of [(chrom, start, end), ...], merge all overlapping regions

    This returns a dict, where values are sorted lists of [start, end].
    """
    bar = sorted(regions)
    out = dict()
    last = [None, None, None]
    for reg in bar:
        if reg[0] == last[0] and reg[1] <= last[1]:
            last[2] = reg[2]
            continue
        else:
            if last[0]:
                if last[0] not in out:
                    out[last[0]] = list()
                out[last[0]].append([last[1], last[2]])
            last = [reg[0], reg[1], reg[2]]
    if last[0] not in out:
        out[last[0]] = list()
    out[last[0]].append([last[1], last[2]])
    return out


def makeTiles(db, args):
    """
    Given a deepBlue object, return a list of regions that will be queried
    """
    out = []
    for (k, v) in db.chromsTuple:
        start = 0
        while start <= v:
            end = start + args.binSize
            if end > v:
                end = v
            out.append([k, start, end])
            start += end + args.distanceBetweenBins
    return out


def makeRegions(args):
    """
    Given a list of BED/GTF files, make a list of regions.
    These are vaguely extended as appropriate. For simplicity, the maximum of --beforeRegionStartLength
    and --afterRegionStartLength are tacked on to each end and transcripts are used for GTF files.
    """
    itree = GTF(args.regionsFileName, transcriptID=args.transcriptID, transcript_id_designator=args.transcript_id_designator)
    o = list()
    extend = max(args.beforeRegionsStartLength, args.afterRegionsStartLength)
    for chrom in GTF.chroms:
        regs = itree.findOverlaps(chrom, 0, 4294967295)  # bigWig files use 32 bit coordinates
        for reg in regs:
            o.append[[chrom, max(0, reg[0] - extend), reg[1] + extend]]
    del itree
    return o


class deepBlue(object):
    def __init__(self, sample, url="http://deepblue.mpi-inf.mpg.de/xmlrpc", userKey="anonymous_key"):
        """
        Connect to the requested deepblue server with the given user key and request the specifed sample from it.

        >>> sample = "S002R5H1.ERX300721.H3K4me3.bwa.GRCh38.20150528.bedgraph"
        >>> db = deepBlue(sample)
        >>> assert(db.chroms("chr1") == 248956422)
        """
        self.sample = sample
        self.url = url
        self.userKey = userKey
        self.server = xmlrpclib.Server(url, allow_none=True)
        self.info = None
        self.experimentID = None
        self.genome = None
        self.chromsDict = None
        self.chromsTuple = None

        # Set self.experimentID
        experimentID = self.getEID()
        if not experimentID:
            raise RuntimeError("The requested sample({}) has no associated experiment!".format(sample))

        # Set self.info
        (status, resp) = self.server.info(self.experimentID, userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching information about '{}': {}".format(resp, sample))
        self.info = resp[0]

        # Set self.genome
        genome = self.getGenome()
        if not genome:
            raise RuntimeError("Unable to determine an appropriate genome for '{}'".format(sample))

        # Set self.chroms
        chroms = self.getChroms()
        if not chroms:
            raise RuntimeError("Unable to determine chromosome names/sizes for '{}'".format(sample))

    def getEID(self):
        """
        Given a sample name, return its associated experiment ID (or None on error).

        self.experimentID is then the internal ID (e.g., e52525)
        """
        (status, resps) = self.server.search(self.sample, "experiments", self.userKey)
        if status != "okay":
            raise RuntimeError("Received an error ({}) while searching for the experiment associated with '{}'".format(resps, self.sample))
        for resp in resps:
            if resp[1] == self.sample:
                self.experimentID = resp[0]
                return resp[0]
        return None

    def getGenome(self):
        """
        Determines and sets the genome assigned to a given sample. On error, this raises a runtime exception.

        self.genome is then the internal genome ID.
        """
        if "genome" in self.info.keys():
            self.genome = self.info["genome"]
        return self.genome

    def getChroms(self):
        """
        Determines and sets the chromosome names/sizes for a given sample. On error, this raises a runtime exception.

        self.chroms is then a dictionary of chromosome:length pairs
        """
        (status, resp) = self.server.chromosomes(self.genome, self.userKey)
        if status != "okay":
            raise RuntimeError("Received an error while fetching chromosome information for '{}': {}".format(self.sample, resp))
        self.chromsDict = {k: v for k, v in resp}
        self.chromsTuple = [(k, v) for k, v in resp]
        return resp

    def chroms(self, chrom=None):
        """
        Like the chroms() function in pyBigWig, returns either chromsDict (chrom is None) or the length of a given chromosome
        """
        if chrom is None:
            return self.chromsDict
        elif chrom in self.chromsDict:
            return self.chromsDict[chrom]
        return None

    def values(self, chrom, start=0, end=0):
        """
        Return a numpy array with values for each base in the requested region.
        If end==0 then the region from start to the end of the chromosome is returned.
        """
        if chrom not in self.chroms():
            raise RuntimeError("'{}' is not a valid chromosome.".format(chrom))

        if end == 0:
            end = self.chromsDict[chrom]

        if start >= end:
            raise RuntimeError("The start position MUST be less then the end position ({} and {})".format(start, end))

        intervals = self.intervals(chrom, start, end)

        # Generate the output
        o = np.empty(end - start)
        o[:] = np.nan
        for interval in intervals:
            s = interval[0] - start
            e = s + interval[1] - interval[0]
            if s < 0:
                s = 0
            if e >= end - start:
                e = end - start
            if e > s:
                o[s:e] = interval[2]

        return o

    def intervals(self, chrom, start=0, end=0):
        """
        Return all intervals overlapping a given region
        """
        if chrom not in self.chroms():
            raise RuntimeError("'{}' is not a valid chromosome.".format(chrom))

        if end == 0:
            end = self.chromsDict[chrom]

        if start >= end:
            raise RuntimeError("The start position MUST be less then the end position ({} and {})".format(start, end))

        # Get the experiment information
        (status, queryID) = self.server.select_experiments(self.sample, chrom, start, end, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching values in the range {}:{}-{} in file '{}': {}".format(chrom, start, end, self.sample, queryID))
        if not queryID:
            raise RuntimeError("Somehow, we received None as a query ID (range {}:{}-{} in file '{}')".format(chrom, start, end, self.sample))

        # Query the regions
        (status, reqID) = self.server.get_regions(queryID, "START,END,VALUE", self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching regions in the range {}:{}-{} in file '{}': {}".format(chrom, start, end, self.sample, reqID))

        # Wait for the server to process the data
        (status, info) = self.server.info(reqID, self.userKey)
        request_status = info[0]["state"]
        while request_status != "done" and request_status != "failed":
            time.sleep(0.1)
            (status, info) = self.server.info(reqID, self.userKey)
            request_status = info[0]["state"]

        # Get the actual data
        (status, resp) = self.server.get_request_data(reqID, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching data in the range {}:{}-{} in file '{}': {}".format(chrom, start, end, self.sample, resp))

        o = []
        for intervals in resp.split("\n"):
            interval = intervals.split("\t")
            if interval[0] == '':
                continue
            o.append((int(interval[0]), int(interval[1]), float(interval[2])))
        return o

    def stats(self, chrom, start=0, end=0):
        """
        Like stats() from pyBigWig, but only ever returns the mean
        """
        ints = self.intervals(chrom, start, end)
        if len(ints) == 0 or np.all(np.isnan(ints)):
            return None
        weights = []
        vals = np.array([x[2] for x in ints])
        for x in ints:
            s = x[0]
            e = x[1]
            if start > 0 and s < start:
                s = start
            if end > 0 and e > end:
                e = end
            weights.append(e - s)
        rv = np.average(vals, weights=np.array(weights))
        return [rv]

    def close(self):
        pass

    def preload(self, regions):
        """
        Given a sample and a set of regions, write a bigWig file containing the underlying signal.

        This function returns the file name, which needs to be deleted by the calling function at some point.
        """
        regions2 = mergeRegions(regions)

        # Make a string out of everything in a resonable order
        regionsStr = ""
        for k, v in self.chromsTuple.items():
            # Munge chromosome names as appropriate
            chrom = mungeChromosome(k, self.chromsDict.key())
            if not chrom:
                continue
            regionsStr += "\n".join(["{} {} {} ".format(chrom, reg[0], reg[1]) for reg in regions2[k]])
            regionsStr += "\n"

        # Send the regions
        (status, regionsID) = self.server.input_regions(self.genome, regionsStr, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching information about '{}': {}".format(resp, self.sample))

        # Make a temporary file
        f = tempfile.NamedTemporaryFile(delete=False)
        fname = f.name
        f.close()

        # Start with the bigWig file
        bw = pyBigWig.open(fname, "w")
        bw.addHeader(self.chromsTuple, maxZooms=0)  # This won't work in IGV!

        # Get the experiment information
        (status, queryID) = self.server.select_experiments(self.sample, None, None, None, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while running select_experiments on file '{}': {}".format(self.sample, queryID))
        if not queryID:
            raise RuntimeError("Somehow, we received None as a query ID (file '{}')".format(self.sample))

        # Intersect
        (status, intersectID) = self.server.intersection(queryID, regionsID, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while running intersection on file '{}': {}".format(self.sample, intersectionID))
        if not intersectID:
            raise RuntimeError("Somehow, we received None as an intersect ID (file '{}')".format(self.sample))

        # Query the regions
        (status, reqID) = self.server.get_regions(intersectID, "CHROM,START,END,VALUE", self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching regions in file '{}': {}".format(self.sample, reqID))

        # Wait for the server to process the data
        (status, info) = self.server.info(reqID, self.userKey)
        request_status = info[0]["state"]
        while request_status != "done" and request_status != "failed":
            time.sleep(0.1)
            (status, info) = self.server.info(reqID, self.userKey)
            request_status = info[0]["state"]

        # Get the actual data
        (status, resp) = self.server.get_request_data(reqID, self.userKey)
        if status != "okay":
            raise RuntimeError("Received the following error while fetching data in file '{}': {}".format(self.sample, resp))

        for intervals in resp.split("\n"):
            interval = intervals.split("\t")
            if interval[0] == '':
                continue
            bw.addEntries([interval[0]], [int(interval[1])], ends=[int(interval[2])], values=[float(interval[3])])
        bw.close()

        return fname
            o.append((int(interval[0]), int(interval[1]), float(interval[2])))
