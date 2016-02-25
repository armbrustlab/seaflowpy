#!/usr/bin/env python

import argparse
import boto3
import botocore
import copy
import errno
import io
import getpass
import gzip
import multiprocessing as mp
import numpy as np
import os
import pandas as pd
import pprint
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time

# Global configuration variables for AWS
# ######################################
# Default name of Seaflow bucket
SEAFLOW_BUCKET = "armbrustlab.seaflow"
# Default AWS region
AWS_REGION = "us-west-2"


def main():
    p = argparse.ArgumentParser(
        description="Filter EVT data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    g_in = p.add_mutually_exclusive_group(required=True)
    g_in.add_argument("--files", nargs="+",
                   help="""EVT file paths. - to read from stdin.
                        (required unless --evt_dir or --s3)""")
    g_in.add_argument("--evt_dir",
                   help="EVT directory path (required unless --files or --s3)")
    g_in.add_argument("--s3", default=False, action="store_true",
                   help="""Read EVT files from s3://seaflowdata/CRUISE where
                        cruise is provided by --cruise (required unless --files
                        or --evt_dir)""")

    p.add_argument("--db",
                   help="""SQLite3 db file. If this file is to be
                        compressed (i.e. --gz_db is set), an extension
                        of ".gz" will automatically be added to the path
                        given here. (required unless --binary_dir)""")
    p.add_argument("--binary_dir",
                   help="""Directory in which to save LabView binary formatted
                        files of focused particles (OPP). Will be created
                        if does not exist. (required unless --db)""")

    p.add_argument("--cruise", required=True, help="Cruise name (required)")
    p.add_argument("--notch1", type=float, help="Notch 1 (optional)")
    p.add_argument("--notch2", type=float, help="Notch 2 (optional)")
    p.add_argument("--width", type=float, default=0.5, help="Width (optional)")
    p.add_argument("--origin", type=float, help="Origin (optional)")
    p.add_argument("--offset", type=float, default=0.0,
                   help="Offset (optional)")

    p.add_argument("--cpus", required=False, type=int, default=1,
                   help="""Number of CPU cores to use in filtering
                        (optional)""")
    p.add_argument("--no_index", default=False, action="store_true",
                   help="Don't create SQLite3 indexes (optional)")
    p.add_argument("--no_opp_db", default=False, action="store_true",
                   help="Don't save OPP data to db (optional)")
    p.add_argument("--gz_db", default=False, action="store_true",
                   help="gzip compress output db (optional)")
    p.add_argument("--gz_binary", default=False, action="store_true",
                   help="gzip compress output binary files (optional)")
    p.add_argument("--progress", type=float, default=10.0,
                   help="Progress update %% resolution (optional)")
    p.add_argument("--limit", type=int, default=None,
                   help="""Limit how many files to process. Useful for testing.
                        (optional)""")

    args = p.parse_args()

    if not args.db and not args.binary_dir:
        sys.stderr.write("At least one of --db or --binary_dir is required\n\n")
        p.print_help()
        sys.exit(1)

    # Print defined parameters
    v = dict(vars(args))
    to_delete = [k for k in v if v[k] is None]
    for k in to_delete:
        v.pop(k, None)  # Remove undefined parameters
    print "\nDefined parameters:"
    pprint.pprint(v, indent=2)

    # Find EVT files
    if args.files:
        files = parse_file_list(args.files)
    elif args.evt_dir:
        files = find_evt_files(args.evt_dir)
    elif args.s3:
        # Make sure try to access S3 up front to setup AWS credentials before
        # launching child processes.
        files = get_s3_files(args.cruise)

    # Restrict length of file list with --limit
    if (not args.limit is None) and (args.limit > 0):
        files = files[:args.limit]

    # Copy --progress to --every alias
    args.every = args.progress

    # Construct kwargs to pass to filter
    kwargs = vars(args)
    filter_keys = ["notch1", "notch2", "width", "offset", "origin"]
    kwargs["filter_options"] = dict((k, kwargs[k]) for k in filter_keys)
    kwargs["files"] = files

    # Filter
    filter_files(**kwargs)
    # Index
    if args.db:
        if not args.no_index:
            ensure_indexes(args.db)
        # Compress Db
        if args.gz_db:
            gzip_file(args.db, print_timing=True)


# ----------------------------------------------------------------------------
# Functions and classes to manage filter workflows
# ----------------------------------------------------------------------------
def filter_files(**kwargs):
    """Filter a list of files.

    Keyword arguments:
        files - paths to files to filter
        cruise - cruise name
        cpus - number of worker processes to use
        filter_options - Dictionary of filter params
            (notch1, notch2, width, offset, origin)
        every - Percent progress output resolution
        s3 - Get EVT data from S3
        no_opp_db - Don't save OPP data to SQLite3 db
        gz_db - Gzip SQLite3 db
        gz_binary - Gzip binary OPP files
        db = SQLite3 db path
        binary_dir = Directory for output binary OPP files
    """
    o = {
        "files": [],
        "cruise": None,
        "cpus": 1,
        "filter_options": {},
        "every": 10.0,
        "s3": False,
        "no_opp_db": False,
        "gz_db": False,
        "gz_binary": False,
        "db": None,
        "binary_dir": None
    }
    o.update(kwargs)

    if o["db"]:
        ensure_tables(o["db"])

    evtcnt = 0
    oppcnt = 0
    files_ok = 0

    # Create a pool of N worker processes
    pool = mp.Pool(o["cpus"])

    # Construct worker inputs
    inputs = []
    files = o.pop("files")
    for f in files:
        inputs.append(copy.copy(o))
        inputs[-1]["file"] = f

    print ""
    print "Filtering %i EVT files. Progress every %i%% (approximately)" % \
        (len(files), o["every"])

    t0 = time.time()

    last = 0  # Last progress milestone in increments of every
    evtcnt_block = 0  # EVT particles in this block (between milestones)
    oppcnt_block = 0  # OPP particles in this block

    # Filter particles in parallel with process pool
    for i, res in enumerate(pool.imap_unordered(do_work, inputs)):
        evtcnt_block += res["evtcnt"]
        oppcnt_block += res["oppcnt"]
        files_ok += 1 if res["ok"] else 0

        # Print progress periodically
        perc = float(i + 1) / len(files) * 100  # Percent completed
        milestone = int(perc / o["every"]) * o["every"]   # Round down to closest every%
        if milestone > last:
            now = time.time()
            evtcnt += evtcnt_block
            oppcnt += oppcnt_block
            try:
                ratio_block = float(oppcnt_block) / evtcnt_block
            except ZeroDivisionError:
                ratio_block = 0.0
            msg = "File: %i/%i (%.02f%%)" % (i + 1, len(files), perc)
            msg += " Particles this block: %i / %i (%.06f) elapsed: %.2fs" % \
                (oppcnt_block, evtcnt_block, ratio_block, now - t0)
            print msg
            last = milestone
            evtcnt_block = 0
            oppcnt_block = 0
    # If any particle count data is left, add it to totals
    if evtcnt_block > 0:
        evtcnt += evtcnt_block
        oppcnt += oppcnt_block

    try:
        opp_evt_ratio = float(oppcnt) / evtcnt
    except ZeroDivisionError:
        opp_evt_ratio = 0.0

    t1 = time.time()
    delta = t1 - t0
    try:
        evtrate = float(evtcnt) / delta
    except ZeroDivisionError:
        evtrate = 0.0
    try:
        opprate = float(oppcnt) / delta
    except ZeroDivisionError:
        opprate = 0.0

    print ""
    print "Input EVT files = %i" % len(files)
    print "Parsed EVT files = %i" % files_ok
    print "EVT particles = %s (%.2f p/s)" % (evtcnt, evtrate)
    print "OPP particles = %s (%.2f p/s)" % (oppcnt, opprate)
    print "OPP/EVT ratio = %.06f" % opp_evt_ratio
    print "Filtering completed in %.2f seconds" % (delta,)


def do_work(options):
    """multiprocessing pool worker function"""
    try:
        return filter_one_file(**options)
    except KeyboardInterrupt as e:
        pass


def filter_one_file(**kwargs):
    """Filter one EVT file, save to sqlite3, return filter stats"""
    o = kwargs
    result = {
        "ok": False,
        "evtcnt": 0,
        "oppcnt": 0,
        "path": o["file"]
    }

    evt_file = o["file"]
    fileobj = None
    if o["s3"]:
        fileobj = download_s3_file_memory(evt_file)

    try:
        evt = EVT(path=evt_file, fileobj=fileobj)
    except EVTFileError as e:
        print "Could not parse file %s: %s" % (evt_file, repr(e))
    except:
        print "Unexpected error for file %s" % evt_file
    else:
        evt.filter(**o["filter_options"])

        if o["db"]:
            evt.save_opp_to_db(o["cruise"], o["db"], no_opp=o["no_opp_db"])

        if o["binary_dir"]:
            # Might have julian day, might not
            outdir = os.path.join(
                o["binary_dir"],
                os.path.dirname(evt.get_julian_path()))
            mkdir_p(outdir)
            outfile = os.path.join(
                o["binary_dir"],
                evt.get_julian_path())
            if o["gz_binary"]:
                outfile += ".gz"
            evt.write_opp_binary(outfile)

        result["ok"] = True
        result["evtcnt"] = evt.evtcnt
        result["oppcnt"] = evt.oppcnt

    return result


class EVT(object):
    """Class for EVT data operations"""

    # EVT file name regexes. Does not contain directory names.
    file_re = re.compile(
        r'^.*?/?'
        r'(?P<julian>\d{4}_\d{1,3})?/?'
        r'(?P<file>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}[+-]\d{2}-?\d{2}|\d+\.evt)'
        r'(?P<gz>\.gz)?$')

    # Data columns
    cols = [
        "time", "pulse_width", "D1", "D2", "fsc_small", "fsc_perp","fsc_big",
        "pe", "chl_small", "chl_big"
    ]
    int_cols = cols[:2]
    float_cols = cols[2:]

    @staticmethod
    def is_evt(path):
        """Does the file specified by this path look like an EVT file?"""
        return bool(EVT.file_re.match(path))

    def __init__(self, path=None, fileobj=None, read_data=True):
        # If fileobj is set, read data from this object. The path will be used
        # to set the file name in the database and detect compression.
        self.path = path  # EVT file path, local or in S3
        self.fileobj = fileobj  # EVT data in file object

        self.headercnt = 0
        self.evtcnt = 0
        self.oppcnt = 0
        self.opp_evt_ratio = 0.0
        self.evt = None
        self.opp = None

        # Set filter params to None
        # Should be set in filter()
        self.notch1 = None
        self.notch2 = None
        self.offset = None
        self.origin = None
        self.width = None

        self.stats = {}  # min, max, mean for each channel of OPP data

        if read_data:
            self.read_evt()

    def __repr__(self):
        keys = [
            "evtcnt", "oppcnt", "notch1", "notch2", "offset", "origin",
            "width", "path", "headercnt"
        ]
        return pprint.pformat({ k: getattr(self, k) for k in keys }, indent=2)

    def __str__(self):
        return self.__repr__()

    def isgz(self):
        return self.path and self.path.endswith(".gz")

    def get_julian_path(self):
        """Get the file path with julian directory.

        If there is no julian directory in path, just return file name. Always
        remove ".gz" extensions.
        """
        m = self.file_re.match(self.path)
        if m:
            if m.group("julian"):
                return os.path.join(m.group("julian"), m.group("file"))
            else:
                return m.group("file")
        else:
            return self.path

    def open(self):
        """Return an EVT file-like object for reading."""
        handle = None
        if self.fileobj:
            if self.isgz():
                handle = gzip.GzipFile(fileobj=self.fileobj)
            else:
                handle = self.fileobj
        else:
            if self.isgz():
                handle = gzip.GzipFile(self.path, "rb")
            else:
                handle = open(self.path, "rb")
        return handle

    def read_evt(self):
        """Read an EVT binary file and return a pandas DataFrame."""
        with self.open() as fh:
            # Particle count (rows of data) is stored in an initial 32-bit
            # unsigned int
            buff = fh.read(4)
            if len(buff) == 0:
                raise EVTFileError("File is empty")
            if len(buff) != 4:
                raise EVTFileError("File has invalid particle count header")
            rowcnt = np.fromstring(buff, dtype="uint32", count=1)[0]
            if rowcnt == 0:
                raise EVTFileError("File has no particle data")
            # Read the rest of the data. Each particle has 12 unsigned
            # 16-bit ints in a row.
            expected_bytes = rowcnt * 12 * 2  # rowcnt * 12 columns * 2 bytes
            buff = fh.read(expected_bytes)
            if len(buff) != expected_bytes:
                raise EVTFileError(
                    "File has incorrect number of data bytes. Expected %i, saw %i" %
                    (expected_bytes, len(buff))
                )
            particles = np.fromstring(buff, dtype="uint16", count=rowcnt*12)
            # Reshape into a matrix of 12 columns and one row per particle
            particles = np.reshape(particles, [rowcnt, 12])
            # Create a Pandas DataFrame. The first two zeroed uint16s from
            # start of each row are left out. These empty ints are an
            # idiosyncrasy of LabVIEW's binary output format. Label each
            # column with a descriptive name.
            self.evt = pd.DataFrame(np.delete(particles, [0, 1], 1),
                                    columns=self.cols)

            # Convert to float64
            self.evt = self.evt.astype(np.float64)

            # Record the original number of particles
            self.evtcnt = len(self.evt.index)

            # Record the number of particles reported in the header
            self.headercnt = rowcnt

    def filter(self, notch1=None, notch2=None, offset=0.0,
               origin=None, width=0.5):
        """Filter EVT particle data."""
        if self.evt is None or self.evtcnt == 0:
            return

        if (width is None) or (offset is None):
            raise ValueError(
                "Must supply width and offset to EVT.filter()"
            )

        # Make sure all params are floats up front to prevent potential
        # python integer division bugs
        offset = float(offset)
        width = float(width)
        if not origin is None:
            origin = float(origin)
        if not notch1 is None:
            notch1 = float(notch1)
        if not notch2 is None:
            notch2 = float(notch2)

        # Correction for the difference in sensitivity between D1 and D2
        if origin is None:
            origin = (self.evt["D2"] - self.evt["D1"]).median()

        # Only keep particles detected by fsc_small
        opp = self.evt[self.evt["fsc_small"] > 1].copy()

        # Filter aligned particles (D1 = D2), with correction for D1 D2
        # sensitivity difference
        alignedD1 = (opp["D1"] + origin) < (opp["D2"] + (width * 10**4))
        alignedD2 = opp["D2"] < (opp["D1"] + origin + (width * 10**4))
        aligned = opp[alignedD1 & alignedD2]

        fsc_small_max = aligned["fsc_small"].max()

        if notch1 is None:
            min1 = aligned[aligned["fsc_small"] == fsc_small_max]["D1"].min()
            max1 = aligned[aligned["D1"] == min1]["fsc_small"].max()
            notch1 = max1 / (min1 + 10000)

        if notch2 is None:
            min2 = aligned[aligned["fsc_small"] == fsc_small_max]["D2"].min()
            max2 = aligned[aligned["D2"] == min2]["fsc_small"].max()
            notch2 = max2 / (min2 + 10000)

        # Filter focused particles (fsc_small > D + notch)
        oppD1 = aligned["fsc_small"] > ((aligned["D1"] * notch1) - (offset * 10**4))
        oppD2 = aligned["fsc_small"] > ((aligned["D2"] * notch2) - (offset * 10**4))
        opp = aligned[oppD1 & oppD2].copy()

        self.opp = opp
        self.oppcnt = len(self.opp.index)
        try:
            self.opp_evt_ratio = float(self.oppcnt) / self.evtcnt
        except ZeroDivisionError:
            self.opp_evt_ratio = 0.0

        self.notch1 = notch1
        self.notch2 = notch2
        self.offset = offset
        self.origin = origin
        self.width = width

    def calc_opp_stats(self):
        """Calculate min, max, sum, mean for each channel of OPP data"""
        if self.oppcnt == 0:
            return

        for col in self.float_cols:
            self.stats[col] = {
                "min": self.opp[col].min(),
                "max": self.opp[col].max(),
                "mean": self.opp[col].mean()
            }

    def transform(self, vals):
        return 10**((vals / 2**16) * 3.5)

    def save_opp_to_db(self, cruise, db, transform=True, no_opp=False):
        if self.oppcnt == 0:
            return

        try:
            if not no_opp:
                self.insert_opp_sqlite3(cruise, db, transform=transform)
            self.insert_filter_sqlite3(cruise, db, transform=transform)
        except:
            raise

    def insert_opp_sqlite3(self, cruise, db, transform=True):
        if self.oppcnt == 0:
            return

        opp = self.create_opp_for_db(cruise, transform=transform)

        sql = "INSERT INTO opp VALUES (%s)" % ",".join("?" * opp.shape[1])
        con = sqlite3.connect(db, timeout=120)
        cur = con.cursor()
        cur.executemany(sql, opp.itertuples(index=False))
        con.commit()

    def insert_filter_sqlite3(self, cruise_name, db, transform=True):
        if self.opp is None or self.evtcnt == 0 or self.oppcnt == 0:
            return

        vals = [cruise_name, self.get_julian_path(), self.oppcnt, self.evtcnt,
            self.opp_evt_ratio, self.notch1, self.notch2, self.offset,
            self.origin, self.width]

        self.calc_opp_stats()
        for channel in self.float_cols:
            if transform:
                vals.append(self.transform(self.stats[channel]["min"]))
                vals.append(self.transform(self.stats[channel]["max"]))
                vals.append(self.transform(self.stats[channel]["mean"]))
            else:
                vals.append(self.stats[channel]["min"])
                vals.append(self.stats[channel]["max"])
                vals.append(self.stats[channel]["mean"])

        sql = "INSERT INTO filter VALUES (%s)" % ",".join("?"*len(vals))
        con = sqlite3.connect(db, timeout=120)
        cur = con.cursor()
        cur.execute(sql, tuple(vals))
        con.commit()

    def create_opp_for_db(self, cruise, transform=True):
        """Return a copy of opp ready for insert into db"""
        if self.opp is None:
            return

        opp = self.opp.copy()

        # Convert int columns to int64
        opp[self.int_cols] = opp[self.int_cols].astype(np.int64)

        # Log transform data scaled to 3.5 decades
        if transform:
            opp[self.float_cols] = self.transform(opp[self.float_cols])

        # Add columns for cruise name, file name, and particle ID to OPP
        opp.insert(0, "cruise", cruise)
        opp.insert(1, "file", self.get_julian_path())
        opp.insert(2, "particle", np.arange(1, self.oppcnt+1, dtype=np.int64))

        return opp

    def write_opp_binary(self, outfile):
        """Write opp to LabView binary file.

        If outfile ends with ".gz", gzip compress.
        """
        if self.oppcnt == 0:
            return

        # Detect gzip output
        gz = False
        if outfile.endswith(".gz"):
            gz = True
            outfile = outfile[:-3]

        with open(outfile, "wb") as fh:
            # Write 32-bit uint particle count header
            header = np.array([self.oppcnt], np.uint32)
            header.tofile(fh)

            # Write particle data
            self.create_opp_for_binary().tofile(fh)

        if gz:
            gzip_file(outfile)

    def create_opp_for_binary(self):
        """Return a copy of opp ready to write to binary file"""
        if self.opp is None:
            return

        # Convert back to original type
        opp = self.opp.astype(np.uint16)

        # Add leading 4 bytes to match LabViews binary format
        zeros = np.zeros([self.oppcnt, 1], dtype=np.uint16)
        tens = np.copy(zeros)
        tens.fill(10)
        opp.insert(0, "tens", tens)
        opp.insert(1, "zeros", zeros)

        return opp.as_matrix()

    def write_opp_csv(self, outfile):
        if self.oppcnt == 0:
            return
        self.opp.to_csv(outfile, sep=",", index=False, header=False)

    def write_evt_csv(self, outfile):
        if self.evt is None:
            return
        self.evt.to_csv(outfile, sep=",", index=False)


# ----------------------------------------------------------------------------
# Functions to manage lists of local EVT files
# ----------------------------------------------------------------------------
def parse_file_list(files):
    files_list = []
    if len(files) and files[0] == "-":
        for line in sys.stdin:
            f = line.rstrip()
            if EVT.is_evt(f):
                files_list.append(f)
    else:
        for f in files:
            if EVT.is_evt(f):
                files_list.append(f)
    return files_list


def find_evt_files(evt_dir):
    evt_files = []

    for root, dirs, files in os.walk(evt_dir):
        for f in files:
            if EVT.is_evt(f):
                evt_files.append(os.path.join(root, f))

    return sorted(evt_files)


# ----------------------------------------------------------------------------
# AWS functions
# ----------------------------------------------------------------------------
def get_aws_credentials():
    aws_access_key_id = getpass.getpass("aws_access_key_id: ")
    aws_secret_access_key = getpass.getpass("aws_secret_access_key: ")
    return (aws_access_key_id, aws_secret_access_key)


def save_aws_credentials(aws_access_key_id, aws_secret_access_key):
    # Make ~/.aws config directory
    awsdir = os.path.join(os.environ["HOME"], ".aws")
    mkdir_p(awsdir)

    flags = os.O_WRONLY | os.O_CREAT

    # Make credentials file
    credentials = os.path.join(awsdir, "credentials")
    with os.fdopen(os.open(credentials, flags, 0600), "w") as fh:
        fh.write("[default]\n")
        fh.write("aws_access_key_id = %s\n" % aws_access_key_id)
        fh.write("aws_secret_access_key = %s\n" % aws_secret_access_key)

    # May as well make config file and set default region while we're at it
    config = os.path.join(awsdir, "config")
    with os.fdopen(os.open(config, flags, 0600), "w") as fh:
        fh.write("[default]\n")
        fh.write("region = %s\n" % AWS_REGION)


def get_s3_connection():
    try:
        s3 = boto3.resource("s3")
    except:
        (aws_access_key_id, aws_secret_access_key) = get_aws_credentials()
        # Save credentials so we don't have to do this all the time
        # And so that any child processes have acces to AWS resources
        save_aws_credentials(aws_access_key_id, aws_secret_access_key)
        s3 = boto3.resource("s3")
    return s3


def get_s3_bucket(s3, bucket_name):
    bucket = s3.Bucket(bucket_name)
    exists = True
    try:
        s3.meta.client.head_bucket(Bucket=bucket_name)
    except botocore.exceptions.ClientError as e:
        # If a client error is thrown, then check that it was a 404 error.
        # If it was a 404 error, then the bucket does not exist.
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            exists = False
    if not exists:
        raise IOError("S3 bucket %s does not exist" % bucket_name)
    return bucket


def get_s3_files(cruise):
    s3 = get_s3_connection()
    bucket = get_s3_bucket(s3, SEAFLOW_BUCKET)
    i = 0
    files = []
    for obj in bucket.objects.filter(Prefix=cruise + "/"):
        # Only keep files for this cruise and skip SFL files
        # Make sure this looks like an EVT file
        if EVT.is_evt(obj.key):
            files.append(obj.key)
    return files


def download_s3_file_memory(key_str, retries=5):
    """Return S3 file contents in io.BytesIO file-like object"""
    tries = 0
    while True:
        try:
            s3 = get_s3_connection()
            obj = s3.Object(SEAFLOW_BUCKET, key_str)
            resp = obj.get()
            data = io.BytesIO(resp["Body"].read())
            return data
        except:
            tries += 1
            if tries == retries:
                raise
            sleep = (2**(tries-1)) + random.random()
            time.sleep(sleep)


# ----------------------------------------------------------------------------
# Database functions
# ----------------------------------------------------------------------------
def ensure_tables(dbpath):
    """Ensure all popcycle tables exists."""
    con = sqlite3.connect(dbpath)
    cur = con.cursor()

    con.execute("""CREATE TABLE IF NOT EXISTS opp (
      -- First three columns are the EVT, OPP, VCT composite key
      cruise TEXT NOT NULL,
      file TEXT NOT NULL,  -- in old files, File+Day. in new files, Timestamp.
      particle INTEGER NOT NULL,
      -- Next we have the measurements. For these, see
      -- https://github.com/fribalet/flowPhyto/blob/master/R/Globals.R and look
      -- at version 3 of the evt header
      time INTEGER NOT NULL,
      pulse_width INTEGER NOT NULL,
      D1 REAL NOT NULL,
      D2 REAL NOT NULL,
      fsc_small REAL NOT NULL,
      fsc_perp REAL NOT NULL,
      fsc_big REAL NOT NULL,
      pe REAL NOT NULL,
      chl_small REAL NOT NULL,
      chl_big REAL NOT NULL,
      PRIMARY KEY (cruise, file, particle)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS vct (
        -- First three columns are the EVT, OPP, VCT, SDS composite key
        cruise TEXT NOT NULL,
        file TEXT NOT NULL,  -- in old files, File+Day. in new files, Timestamp.
        particle INTEGER NOT NULL,
        -- Next we have the classification
        pop TEXT NOT NULL,
        method TEXT NOT NULL,
        PRIMARY KEY (cruise, file, particle)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS filter (
        cruise TEXT NOT NULL,
        file TEXT NOT NULL,
        opp_count INTEGER NOT NULL,
        evt_count INTEGER NOT NULL,
        opp_evt_ratio REAL NOT NULL,
        notch1 REAL NOT NULL,
        notch2 REAL NOT NULL,
        offset REAL NOT NULL,
        origin REAL NOT NULL,
        width REAL NOT NULL,
        D1_min REAL NOT NULL,
        D1_max REAL NOT NULL,
        D1_mean REAL NOT NULL,
        D2_min REAL NOT NULL,
        D2_max REAL NOT NULL,
        D2_mean REAL NOT NULL,
        fsc_small_min REAL NOT NULL,
        fsc_small_max REAL NOT NULL,
        fsc_small_mean REAL NOT NULL,
        fsc_perp_min REAL NOT NULL,
        fsc_perp_max REAL NOT NULL,
        fsc_perp_mean REAL NOT NULL,
        fsc_big_min REAL NOT NULL,
        fsc_big_max REAL NOT NULL,
        fsc_big_mean REAL NOT NULL,
        pe_min REAL NOT NULL,
        pe_max REAL NOT NULL,
        pe_mean REAL NOT NULL,
        chl_small_min REAL NOT NULL,
        chl_small_max REAL NOT NULL,
        chl_small_mean REAL NOT NULL,
        chl_big_min REAL NOT NULL,
        chl_big_max REAL NOT NULL,
        chl_big_mean REAL NOT NULL,
        PRIMARY KEY (cruise, file)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS sfl (
        --First two columns are the SFL composite key
        cruise TEXT NOT NULL,
        file TEXT NOT NULL,  -- in old files, File+Day. in new files, Timestamp.
        date TEXT,
        file_duration REAL,
        lat REAL,
        lon REAL,
        conductivity REAL,
        salinity REAL,
        ocean_tmp REAL,
        par REAL,
        bulk_red REAL,
        stream_pressure REAL,
        flow_rate REAL,
        event_rate REAL,
        PRIMARY KEY (cruise, file)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS stats (
        cruise TEXT NOT NULL,
        file TEXT NOT NULL,
        time TEXT,
        lat REAL,
        lon REAL,
        opp_evt_ratio REAL,
        flow_rate REAL,
        file_duration REAL,
        pop TEXT NOT NULL,
        n_count INTEGER,
        abundance REAL,
        fsc_small REAL,
        chl_small REAL,
        pe REAL,
        PRIMARY KEY (cruise, file, pop)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS cytdiv (
        cruise TEXT NOT NULL,
        file TEXT NOT NULL,
        N0 INTEGER,
        N1 REAL,
        H REAL,
        J REAL,
        opp_red REAL,
        PRIMARY KEY (cruise, file)
    )""")

    con.commit()
    con.close()


def ensure_indexes(dbpath):
    """Create table indexes."""
    t0 = time.time()

    print ""
    print "Creating DB indexes"
    con = sqlite3.connect(dbpath)
    cur = con.cursor()
    index_cmds = [
        "CREATE INDEX IF NOT EXISTS oppFileIndex ON opp (file)",
        "CREATE INDEX IF NOT EXISTS oppFsc_smallIndex ON opp (fsc_small)",
        "CREATE INDEX IF NOT EXISTS oppPeIndex ON opp (pe)",
        "CREATE INDEX IF NOT EXISTS oppChl_smallIndex ON opp (chl_small)",
        "CREATE INDEX IF NOT EXISTS vctFileIndex ON vct (file)",
        "CREATE INDEX IF NOT EXISTS sflDateIndex ON sfl (date)"
    ]
    for cmd in index_cmds:
        cur.execute(cmd)
    con.commit()
    con.close()

    t1 = time.time()
    print "Index creation completed in %.2f seconds" % (t1 - t0,)


# ----------------------------------------------------------------------------
# Utility functions
# ----------------------------------------------------------------------------
def gzip_file(path, print_timing=False):
    gzipbin = "pigz"  # Default to using pigz
    devnull = open(os.devnull, "w")
    try:
        subprocess.check_call(["pigz", "--version"], stdout=devnull,
                              stderr=subprocess.STDOUT)
    except OSError as e:
        # If pigz is not installed fall back to gzip
        gzipbin = "gzip"

    if print_timing:
        t0 = time.time()
        print ""
        print "Compressing %s" % path

    try:
        output = subprocess.check_output([gzipbin, "-f", path],
                                         stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        raise PoolCalledProcessError(e.output)

    if print_timing:
        t1 = time.time()
        print "Compression completed in %.2f seconds" % (t1 - t0)


def mkdir_p(path):
    """Create directory tree for path.

    Doesn't raise an error if a directory in the path already exists.

    From http://stackoverflow.com/questions/600268/mkdir-p-functionality-in-python
    """
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def splitpath(path):
    """Return a list of all path components"""
    parts = []
    path, last = os.path.split(path)
    if last != "":
        parts.append(last)
    while True:
        path, last = os.path.split(path)
        if last != "":
            parts.append(last)
        else:
            if path != "":
                parts.append(path)
            break
    return parts[::-1]


# ----------------------------------------------------------------------------
# Custom exception classes
# ----------------------------------------------------------------------------
class EVTFileError(Exception):
    """Custom exception class for EVT file format errors"""
    pass


class PoolCalledProcessError(Exception):
    """Custom exception to replace subprocess.CalledProcessError

    subprocess.CalledProcessError does not handling pickling/unpickling through
    a multiprocessing pool very well (https://bugs.python.org/issue9400).
    """
    pass


if __name__ == "__main__":
    main()
