import socket
from os import kill, getpid
from Queue import Full
from multiprocessing import Process
from struct import Struct, unpack, calcsize
from msgpack import unpackb

import logging
import settings

import trollius
from trollius import From

logger = logging.getLogger("HorizonLog")

##SafeUnpickler taken from Carbon: https://github.com/graphite-project/carbon/blob/master/lib/carbon/util.py
import sys
try:
  from cStringIO import StringIO
except ImportError:
  from StringIO import StringIO

try:
  import cPickle as pickle
  USING_CPICKLE = True
except:
  import pickle
  USING_CPICKLE = False

# This whole song & dance is due to pickle being insecure
# yet performance critical for carbon. We leave the insecure
# mode (which is faster) as an option (USE_INSECURE_UNPICKLER).
# The SafeUnpickler classes were largely derived from
# http://nadiana.com/python-pickle-insecure
if USING_CPICKLE:
  class SafeUnpickler(object):
    PICKLE_SAFE = {
      'copy_reg' : set(['_reconstructor']),
      '__builtin__' : set(['object']),
    }

    @classmethod
    def find_class(cls, module, name):
      if not module in cls.PICKLE_SAFE:
        raise pickle.UnpicklingError('Attempting to unpickle unsafe module %s' % module)
      __import__(module)
      mod = sys.modules[module]
      if not name in cls.PICKLE_SAFE[module]:
        raise pickle.UnpicklingError('Attempting to unpickle unsafe class %s' % name)
      return getattr(mod, name)

    @classmethod
    def loads(cls, pickle_string):
      pickle_obj = pickle.Unpickler(StringIO(pickle_string))
      pickle_obj.find_global = cls.find_class
      return pickle_obj.load()

else:
  class SafeUnpickler(pickle.Unpickler):
    PICKLE_SAFE = {
      'copy_reg' : set(['_reconstructor']),
      '__builtin__' : set(['object']),
    }
    def find_class(self, module, name):
      if not module in self.PICKLE_SAFE:
        raise pickle.UnpicklingError('Attempting to unpickle unsafe module %s' % module)
      __import__(module)
      mod = sys.modules[module]
      if not name in self.PICKLE_SAFE[module]:
        raise pickle.UnpicklingError('Attempting to unpickle unsafe class %s' % name)
      return getattr(mod, name)

    @classmethod
    def loads(cls, pickle_string):
      return cls(StringIO(pickle_string)).load()
##//SafeUnpickler

class Listen(Process):
    """
    The listener is responsible for listening on a port.
    """
    def __init__(self, port, queue, parent_pid, type="pickle"):
        super(Listen, self).__init__()
        try:
            self.ip = settings.HORIZON_IP
        except AttributeError:
            # Default for backwards compatibility
            self.ip = socket.gethostname()
        self.port = port
        self.q = queue
        self.daemon = True
        self.parent_pid = parent_pid
        self.current_pid = getpid()
        self.type = type

        ##Use the safe unpickler that comes with carbon rather than standard python pickle/cpickle
        self.unpickler = SafeUnpickler

    def gen_unpickle(self, infile):
        """
        Generate a pickle from a stream
        """
        try:
            bunch = self.unpickler.loads(infile)
            yield bunch
        except EOFError:
            return

    def read_all(self, sock, n):
        """
        Read n bytes from a stream
        """
        data = ''
        while n > 0:
            buf = sock.recv(n)
            n -= len(buf)
            data += buf
        return data

    def check_if_parent_is_alive(self):
        """
        Self explanatory
        """
        try:
            kill(self.current_pid, 0)
            kill(self.parent_pid, 0)
        except:
            exit(0)

    def listen_pickle(self):
        """
        Listen for pickles over tcp
        """
        @trollius.coroutine
        def handle_pickles(reader, writer):
            chunk = []
            while True:
                self.check_if_parent_is_alive()
                try:
                    header = yield From(reader.readexactly(calcsize('!I')))
                    length, = Struct('!I').unpack(header)
                    body = yield From(reader.readexactly(length))

                    # Iterate and chunk each individual datapoint
                    for bunch in self.gen_unpickle(body):
                        for metric in bunch:
                            chunk.append(metric)

                            # Queue the chunk and empty the variable
                            if len(chunk) > settings.CHUNK_SIZE:
                                try:
                                    self.q.put(list(chunk), block=False)
                                    chunk[:] = []

                                # Drop chunk if queue is full
                                except Full:
                                    logger.info('queue is full, dropping datapoints')
                                    chunk[:] = []

                except Exception as e:
                    logger.exception('Processing metrics')
                    raise

        loop = trollius.get_event_loop()
        server = loop.run_until_complete(trollius.start_server(handle_pickles,
                    self.ip, self.port, loop=loop))
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            pass

        # Close the server
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()

    def listen_udp(self):
        """
        Listen over udp for MessagePack strings
        """
        while 1:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind((self.ip, self.port))
                logger.info('listening over udp for messagepack on %s' % self.port)

                chunk = []
                while 1:
                    self.check_if_parent_is_alive()
                    data, addr = s.recvfrom(1024)
                    metric = unpackb(data)
                    chunk.append(metric)

                    # Queue the chunk and empty the variable
                    if len(chunk) > settings.CHUNK_SIZE:
                        try:
                            self.q.put(list(chunk), block=False)
                            chunk[:] = []

                        # Drop chunk if queue is full
                        except Full:
                            logger.info('queue is full, dropping datapoints')
                            chunk[:] = []

            except Exception as e:
                logger.info('can\'t connect to socket: ' + str(e))
                break

    def run(self):
        """
        Called when process intializes.
        """
        logger.info('started listener')

        if self.type == 'pickle':
            self.listen_pickle()
        elif self.type == 'udp':
            self.listen_udp()
        else:
            logging.error('unknown listener format')
