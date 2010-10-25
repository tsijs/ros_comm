# Software License Agreement (BSD License)
#
# Copyright (c) 2008, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# Revision $Id$

"""
rospy implementation of topics.

Client API
==========

L{Publisher} and L{Subscriber} are the client API for topics.

Internal Implementation
=======================

Internally, L{_TopicImpl} instances (_PublisherImpl/_SubscriberImpl)
are used to manage actual transport connections.  The L{_TopicManager}
is responsible for tracking the system-wide state of publications and
subscriptions as well as the L{_TopicImpl} instances. More info is below.
 
L{_TopicManager}
================

The L{_TopicManager} does the backend topic bookkeeping for the local
node.  Use L{get_topic_manager()} to access singleton. Actual topic
implementations are done through the
L{_TopicImpl}/L{_PublisherImpl}/L{_SubscriberImpl} hierarchy. Client
code generates instances of type L{Publisher}/L{Subscriber}, which
enable to client to create multiple publishers/subscribers of that
topic that get controlled access to the underlying share connections.

Common parent classes for all rospy topics. The rospy topic autogenerators
create classes that are children of these implementations.
"""

from __future__ import with_statement
import struct, cStringIO, thread, threading, logging, time
from itertools import chain
import traceback

import roslib.message

from rospy.core import *
from rospy.exceptions import ROSSerializationException, TransportTerminated
from rospy.msg import serialize_message, args_kwds_to_message

from rospy.impl.registration import get_topic_manager, set_topic_manager, Registration, get_registration_listeners
from rospy.impl.tcpros import get_tcpros_handler, DEFAULT_BUFF_SIZE
from rospy.impl.transport import DeadTransport

_logger = logging.getLogger('rospy.topics')

# wrap roslib implementation and map it to rospy namespace
Message = roslib.message.Message

#######################################################################
# Base classes for all client-API instantiated pub/sub
#
# There are two trees: Topic and _TopicImpl. Topic is the client API
# for interfacing with topics, while _TopicImpl implements the
# underlying connection details. 

class Topic(object):
    """Base class of L{Publisher} and L{Subscriber}"""
    
    def __init__(self, name, data_class, reg_type):
        """
        @param name: graph resource name of topic, e.g. 'laser'. 
        @type  name: str
        @param data_class: message class for serialization
        @type  data_class: L{Message}
        @param reg_type Registration.PUB or Registration.SUB
        @type  reg_type: str
        @raise ValueError: if parameters are invalid
        """
        
        if not name or not isinstance(name, basestring):
            raise ValueError("topic name is not a non-empty string")
        if isinstance(name, unicode):
            raise ValueError("topic name cannot be unicode")
        if data_class is None:
            raise ValueError("topic parameter 'data_class' is not initialized")
        if not type(data_class) == type:
            raise ValueError("data_class [%s] is not a class"%data_class) 
        if not issubclass(data_class, roslib.message.Message):
            raise ValueError("data_class [%s] is not a message data class"%data_class.__class__.__name__)
        # #2202
        if not roslib.names.is_legal_name(name):
            import warnings
            warnings.warn("'%s' is not a legal ROS graph resource name. This may cause problems with other ROS tools"%name, stacklevel=2)
        
        # this is a bit ugly, but necessary due to the fact that we allow
        # topics and services to be initialized before the node
        if not rospy.core.is_initialized():
            self.resolved_name = rospy.names.resolve_name_without_node_name(name)
        else:
            # init_node() has been called, so we can do normal resolution
            self.resolved_name = resolve_name(name)

        self.name = self.resolved_name # #1810 for backwards compatibility

        self.data_class = data_class
        self.type = data_class._type
        self.md5sum = data_class._md5sum
        self.reg_type = reg_type
        self.impl = get_topic_manager().acquire_impl(reg_type, self.resolved_name, data_class)

    def get_num_connections(self):
        """
        get the number of connections to other ROS nodes for this topic. For a Publisher,
        this corresponds to the number of nodes subscribing. For a Subscriber, the number
        of publishers.
        @return: number of connections
        @rtype: int
        """
        return self.impl.get_num_connections()
        
    def unregister(self):
        """
        unpublish/unsubscribe from topic. Topic instance is no longer
        valid after this call. Additional calls to unregister() have no effect.
        """
        # as we don't guard unregister, have to protect value of
        # resolved_name for release_impl call
        resolved_name = self.resolved_name
        if resolved_name and self.impl:
            get_topic_manager().release_impl(self.reg_type, resolved_name)
            self.impl = self.resolved_name = self.type = self.md5sum = self.data_class = None

class _TopicImpl(object):
    """
    Base class of internal topic implementations. Each topic has a
    singleton _TopicImpl implementation for managing the underlying
    connections.
    """
    
    def __init__(self, name, data_class):
        """
        Base constructor
        @param name: graph resource name of topic, e.g. 'laser'. 
        @type  name: str
        @param data_class: message data class 
        @type  data_class: L{Message}
        """

        # #1810 made resolved/unresolved more explicit so we don't accidentally double-resolve
        self.resolved_name = resolve_name(name) #NOTE: remapping occurs here!
        self.name = self.resolved_name # for backwards compatibility
        
        self.data_class = data_class
        self.type = data_class._type
        self.handler = None
        self.seq = 0
        # lock is used for to serialize call order that methods that
        # modify self.connections. Because add/removing connections is
        # a rare event, we go through the extra hassle of making a
        # copy of the connections/dead_connections/callbacks lists
        # when modifying, then setting the reference to the new copy.
        # With this pattern, other code can use these lists without
        # having to acquire c_lock
        self.c_lock = threading.RLock()
        self.connections = []
        self.closed = False
        # number of Topic instances using this
        self.ref_count = 0
        #STATS
        self.dead_connections = [] #for retaining stats on old conns

    def __del__(self):
        # very similar to close(), but have to be more careful in a __del__ what we call
        if self.closed:
            return
        if self.connections is not None:
            for c in self.connections:
                try:
                    c.close()
                except:
                    pass
            del self.connections[:]
            del self.dead_connections[:]            
        self.c_lock = self.dead_connections = self.connections = self.handler = self.data_class = self.type = None

    def close(self):
        """close I/O"""
        if self.closed:
            return
        self.closed = True
        if self.c_lock is not None:
            with self.c_lock:
                for c in self.connections:
                    try:
                        if c is not None:
                            c.close()
                    except:
                        # seems more logger.error internal than external logerr
                        _logger.error(traceback.format_exc())
                del self.connections[:]
            self.c_lock = self.connections = self.handler = self.data_class = self.type = None
            
            # note: currently not deleting self.dead_connections. not
            # sure what the right policy here as dead_connections is
            # for statistics only. they should probably be moved to
            # the topic manager instead.

    def get_num_connections(self):
        with self.c_lock:
            return len(self.connections)
    
    def has_connection(self, endpoint_id):
        """
        Query whether or not a connection with the associated \a
        endpoint has been added to this object.
        @param endpoint_id: endpoint ID associated with connection. 
        @type  endpoint_id: str
        """
        # save reference to avoid lock
        conn = self.connections
        for c in conn:
            if c.endpoint_id == endpoint_id:
                return True
        return False

    def has_connections(self):
        """
        Check to see if this topic is connected to other publishers/subscribers 
        @return: True if topic is connected
        @rtype: bool
        """
        if self.connections:
            return True
        return False

    def add_connection(self, c):
        """
        Add a connection to this topic. 
        @param c: connection instance
        @type  c: Transport
        @return: True if connection was added
        @rtype: bool
        """
        with self.c_lock:
            # c_lock is to make add_connection thread-safe, but we
            # still make a copy of self.connections so that the rest of the
            # code can use self.connections in an unlocked manner
            new_connections = self.connections[:]
            new_connections.append(c)
            self.connections = new_connections

            # connections make a callback when closed
            c.set_cleanup_callback(self.remove_connection)
            
            return True

    def remove_connection(self, c):
        """
        Remove connection from topic.
        @param c: connection instance to remove
        @type  c: Transport
        """
        try:
            # c_lock is to make remove_connection thread-safe, but we
            # still make a copy of self.connections so that the rest of the
            # code can use self.connections in an unlocked manner
            self.c_lock.acquire()
            new_connections = self.connections[:]
            new_dead_connections = self.dead_connections[:]                        
            if c in new_connections:
                new_connections.remove(c)
                new_dead_connections.append(DeadTransport(c))
            self.connections = new_connections
            self.dead_connections = new_dead_connections
        finally:
            self.c_lock.release()

    def get_stats_info(self): # STATS
        """
        Get the stats for this topic
        @return: stats for topic in getBusInfo() format::
          ((connection_id, destination_caller_id, direction, transport, topic_name, connected)*)
        @rtype: list
        """
        # save referenceto avoid locking
        connections = self.connections
        dead_connections = self.dead_connections
        return [(c.id, c.endpoint_id, c.direction, c.transport_type, self.resolved_name, True) for c in connections] + \
               [(c.id, c.endpoint_id, c.direction, c.transport_type, self.resolved_name, False) for c in dead_connections]

    def get_stats(self): # STATS
        """Get the stats for this topic (API stub)"""
        raise Exception("subclasses must override")

#  Implementation note: Subscriber attaches to a _SubscriberImpl
#  singleton for that topic.  The underlying impl manages the
#  connections for that publication and enables thread-safe access

class Subscriber(Topic):
    """
    Class for registering as a subscriber to a specified topic, where
    the messages are of a given type.
    """
    def __init__(self, name, data_class, callback=None, callback_args=None,
                 queue_size=None, buff_size=DEFAULT_BUFF_SIZE, tcp_nodelay=False):
        """
        Constructor.

        NOTE: for the queue_size and buff_size
        parameters, rospy does not attempt to do intelligent merging
        between multiple Subscriber instances for the same topic. As
        they share the same underlying transport, multiple Subscribers
        to the same topic can conflict with one another if they set
        these parameters differently.

        @param name: graph resource name of topic, e.g. 'laser'.
        @type  name: str
        @param data_class: data type class to use for messages,
          e.g. std_msgs.msg.String
        @type  data_class: L{Message} class
        @param callback: function to call ( fn(data)) when data is
          received. If callback_args is set, the function must accept
          the callback_args as a second argument, i.e. fn(data,
          callback_args).  NOTE: Additional callbacks can be added using
          add_callback().
        @type  callback: str
        @param callback_args: additional arguments to pass to the
          callback. This is useful when you wish to reuse the same
          callback for multiple subscriptions.
        @type  callback_args: any
        @param queue_size: maximum number of messages to receive at
          a time. This will generally be 1 or None (infinite,
          default). buff_size should be increased if this parameter
          is set as incoming data still needs to sit in the incoming
          buffer before being discarded. Setting queue_size
          buff_size to a non-default value affects all subscribers to
          this topic in this process.
        @type  queue_size: int
        @param buff_size: incoming message buffer size in bytes. If
          queue_size is set, this should be set to a number greater
          than the queue_size times the average message size. Setting
          buff_size to a non-default value affects all subscribers to
          this topic in this process.
        @type  buff_size: int
        @param tcp_nodelay: if True, request TCP_NODELAY from
          publisher.  Use of this option is not generally recommended
          in most cases as it is better to rely on timestamps in
          message data. Setting tcp_nodelay to True enables TCP_NODELAY
          for all subscribers in the same python process.
        @type  tcp_nodelay: bool
        @raise ROSException: if parameters are invalid
        """
        super(Subscriber, self).__init__(name, data_class, Registration.SUB)
        #add in args that factory cannot pass in

        # last person to set these to non-defaults wins, not much way
        # around this
        if queue_size is not None:
            self.impl.set_queue_size(queue_size)
        if buff_size != DEFAULT_BUFF_SIZE:
            self.impl.set_buff_size(buff_size)

        if callback is not None:
            # #1852
            # it's important that we call add_callback so that the
            # callback can be invoked with any latched messages
            self.impl.add_callback(callback, callback_args)
            # save arguments for unregister
            self.callback = callback
            self.callback_args = callback_args
        else:
            # initialize fields
            self.callback = self.callback_args = None            
        if tcp_nodelay:
            self.impl.set_tcp_nodelay(tcp_nodelay)        

    def unregister(self):
        """
        unpublish/unsubscribe from topic. Topic instance is no longer
        valid after this call. Additional calls to unregister() have no effect.
        """
        if self.impl:
            # It's possible to have a Subscriber instance with no
            # associated callback
            if self.callback is not None:
                self.impl.remove_callback(self.callback, self.callback_args)
            self.callback = self.callback_args = None
            super(Subscriber, self).unregister()
            
class _SubscriberImpl(_TopicImpl):
    """
    Underyling L{_TopicImpl} implementation for subscriptions.
    """

    def __init__(self, name, data_class):
        """
        ctor.
        @param name: graph resource name of topic, e.g. 'laser'.
        @type  name: str
        @param data_class: Message data class
        @type  data_class: L{Message} class
        """
        super(_SubscriberImpl, self).__init__(name, data_class)
        # client-methods to invoke on new messages. should only modify
        # under lock. This is a list of 2-tuples (fn, args), where
        # args are additional arguments for the callback, or None
        self.callbacks = [] 
        self.queue_size = None
        self.buff_size = DEFAULT_BUFF_SIZE
        self.tcp_nodelay = False

    def close(self):
        """close I/O and release resources"""
        _TopicImpl.close(self)
        if self.callbacks:
            del self.callbacks[:]
            self.callbacks = None
        
    def set_tcp_nodelay(self, tcp_nodelay):
        """
        Set the value of TCP_NODELAY, which causes the Nagle algorithm
        to be disabled for future topic connections, if the publisher
        supports it.
        """
        self.tcp_nodelay = tcp_nodelay
        
    def set_queue_size(self, queue_size):
        """
        Set the receive queue size. If more than queue_size messages
        are waiting to be deserialized, they are discarded.
        
        @param queue_size int: incoming queue size. Must be positive integer or None.
        @type  queue_size: int
        """
        if queue_size == -1:
            self.queue_size = None
        elif queue_size == 0:
            raise ROSException("queue size may not be set to zero")
        elif queue_size is not None and type(queue_size) != int:
            raise ROSException("queue size must be an integer")
        else:
            self.queue_size = queue_size

    def set_buff_size(self, buff_size):
        """
        Set the receive buffer size. The exact meaning of this is
        transport dependent.
        @param buff_size: receive buffer size
        @type  buff_size: int
        """
        if type(buff_size) != int:
            raise ROSException("buffer size must be an integer")
        elif buff_size <= 0:
            raise ROSException("buffer size must be a positive integer")
        self.buff_size = buff_size
        
    def get_stats(self): # STATS
        """
        Get the stats for this topic subscriber
        @return: stats for topic in getBusStats() publisher format::
           (topicName, connStats)
        where connStats is::
           [connectionId, bytesReceived, numSent, dropEstimate, connected]*
        @rtype: list
        """
        # save reference to avoid locking
        conn = self.connections
        dead_conn = self.dead_connections        
        #for now drop estimate is -1
        stats = (self.resolved_name, 
                 [(c.id, c.stat_bytes, c.stat_num_msg, -1, not c.done)
                  for c in chain(conn, dead_conn)] )
        return stats

    def add_callback(self, cb, cb_args):
        """
        Register a callback to be invoked whenever a new message is received
        @param cb: callback function to invoke with message data
          instance, i.e. fn(data). If callback args is set, they will
          be passed in as the second argument.
        @type  cb: fn(msg)
        @param cb_cargs: additional arguments to pass to callback
        @type  cb_cargs: Any
        """
        with self.c_lock:
            # we lock in order to serialize calls to add_callback, but
            # we copy self.callbacks so we can it
            new_callbacks = self.callbacks[:]
            new_callbacks.append((cb, cb_args))
            self.callbacks = new_callbacks

        # #1852: invoke callback with any latched messages
        for c in self.connections:
            if c.latch is not None:
                self._invoke_callback(c.latch, cb, cb_args)

    def remove_callback(self, cb, cb_args):
        """
        Unregister a message callback.
        @param cb: callback function 
        @type  cb: fn(msg)
        @param cb_cargs: additional arguments associated with callback
        @type  cb_cargs: Any
        @raise KeyError: if no matching callback
        """
        with self.c_lock:
            # we lock in order to serialize calls to add_callback, but
            # we copy self.callbacks so we can it
            matches = [x for x in self.callbacks if x[0] == cb and x[1] == cb_args]
            if matches:
                new_callbacks = self.callbacks[:]
                # remove the first match
                new_callbacks.remove(matches[0])
                self.callbacks = new_callbacks
        if not matches:
            raise KeyError("no matching cb")

    def _invoke_callback(self, msg, cb, cb_args):
        """
        Invoke callback on msg. Traps and logs any exceptions raise by callback
        @param msg: message data
        @type  msg: L{Message}
        @param cb: callback
        @type  cb: fn(msg, cb_args)
        @param cb_args: callback args or None
        @type  cb_args: Any
        """
        try:
            if cb_args is not None:
                cb(msg, cb_args)
            else:
                cb(msg)
        except Exception, e:
            if not is_shutdown():
                logerr("bad callback: %s\n%s"%(cb, traceback.format_exc()))
            else:
                _logger.warn("during shutdown, bad callback: %s\n%s"%(cb, traceback.format_exc()))
        
    def receive_callback(self, msgs):
        """
        Called by underlying connection transport for each new message received
        @param msgs: message data
        @type msgs: [L{Message}]
        """
        # save reference to avoid lock
        callbacks = self.callbacks
        for msg in msgs:
            for cb, cb_args in callbacks:
                self._invoke_callback(msg, cb, cb_args)

class SubscribeListener(object):
    """
    Callback API to receive notifications when new subscribers
    connect and disconnect.
    """

    def peer_subscribe(self, topic_name, topic_publish, peer_publish):
        """
        callback when a peer has subscribed from a topic
        @param topic_name: topic name. NOTE: topic name will be resolved/remapped
        @type  topic_name: str
        @param topic_publish: method to publish message data to all subscribers
        @type  topic_publish: fn(data)
        @param peer_publish: method to publish message data to
          new subscriber.  NOTE: behavior for the latter is
          transport-dependent as some transports may be broadcast only.
        @type  peer_publish: fn(data)
        """
        pass

    def peer_unsubscribe(self, topic_name, num_peers):
        """
        callback when a peer has unsubscribed from a topic
        @param topic_name: topic name. NOTE: topic name will be resolved/remapped
        @type  topic_name: str
        @param num_peers: number of remaining peers subscribed to topic
        @type  num_peers: int
        """
        pass


#  Implementation note: Publisher attaches to a
#  _PublisherImpl singleton for that topic.  The underlying impl
#  manages the connections for that publication and enables
#  thread-safe access

class Publisher(Topic):
    """
    Class for registering as a publisher of a ROS topic.
    """

    def __init__(self, name, data_class, subscriber_listener=None, tcp_nodelay=False, latch=False, headers=None):
        """
        Constructor
        @param name: resource name of topic, e.g. 'laser'. 
        @type  name: str
        @param data_class: message class for serialization
        @type  data_class: L{Message} class
        @param subscriber_listener: listener for
          subscription events. May be None.
        @type  subscriber_listener: L{SubscribeListener}
        @param tcp_nodelay: If True, sets TCP_NODELAY on
          publisher's socket (disables Nagle algorithm). This results
          in lower latency publishing at the cost of efficiency.
        @type  tcp_nodelay: bool
        @param latch: If True, the last message published is
        'latched', meaning that any future subscribers will be sent
        that message immediately upon connection.
        @type  latch: bool
        @raise ROSException: if parameters are invalid     
        """
        super(Publisher, self).__init__(name, data_class, Registration.PUB)

        if subscriber_listener:
            self.impl.add_subscriber_listener(subscriber_listener)
        if tcp_nodelay:
            get_tcpros_handler().set_tcp_nodelay(self.resolved_name, tcp_nodelay)
        if latch:
            self.impl.enable_latch()
        if headers:
            self.impl.add_headers(headers)
            
    def publish(self, *args, **kwds):
        """
        Publish message data object to this topic. 
        Publish can either be called with the message instance to
        publish or with the constructor args for a new Message
        instance, i.e.::
          pub.publish(message_instance)
          pub.publish(message_field_1, message_field_2...)            
          pub.publish(message_field_1='foo', message_field_2='bar')
    
        @param args : L{Message} instance, message arguments, or no args if keyword arguments are used
        @param kwds : Message keyword arguments. If kwds are used, args must be unset
        @raise ROSException: If rospy node has not been initialized
        @raise ROSSerializationException: If unable to serialize
        message. This is usually a type error with one of the fields.
        """
        if self.impl is None:
            raise ROSException("publish() to an unregistered() handle")
        if not is_initialized():
            raise ROSException("ROS node has not been initialized yet. Please call init_node() first")
        data = args_kwds_to_message(self.data_class, args, kwds)
        try:
            self.impl.acquire()
            self.impl.publish(data)
        except roslib.message.SerializationError, e:
            # can't go to rospy.logerr(), b/c this could potentially recurse
            _logger.error(traceback.format_exc(e))
            raise ROSSerializationException(str(e))
        finally:
            self.impl.release()            

class _PublisherImpl(_TopicImpl):
    """
    Underyling L{_TopicImpl} implementation for publishers.
    """
    
    def __init__(self, name, data_class):
        """
        @param name: name of topic, e.g. 'laser'. 
        @type  name: str
        @param data_class: Message data class    
        @type  data_class: L{Message} class
        """
        super(_PublisherImpl, self).__init__(name, data_class)
        self.buff = cStringIO.StringIO()
        self.publock = threading.RLock() #for acquire()/release
        self.subscriber_listeners = []

        # additional client connection headers
        self.headers = {}
        
        # publish latch, starts disabled
        self.is_latch = False
        self.latch = None
        
        #STATS
        self.message_data_sent = 0

    def close(self):
        """close I/O and release resources"""
        _TopicImpl.close(self)
        # release resources
        if self.subscriber_listeners:
            del self.subscriber_listeners[:]
        if self.headers:
            self.headers.clear()
        if self.buff is not None:
            self.buff.close()
        self.publock = self.headers = self.buff = self.subscriber_listeners = None

    def add_headers(self, headers):
        """
        Add connection headers to this Topic for future connections.
        @param headers: key/values will be added to current connection
        header set, overriding any existing keys if they conflict.
        @type  headers: dict
        """
        self.headers.update(headers)
    
    def enable_latch(self):
        """
        Enable publish() latch. The latch contains the last published
        message and is sent to any new subscribers.
        """
        self.is_latch = True
        
    def get_stats(self): # STATS
        """
        Get the stats for this topic publisher
        @return: stats for topic in getBusStats() publisher format::
          [topicName, messageDataBytes, connStats],
        where connStats is::
          [id, bytes, numMessages, connected]*
        @rtype: list
        """
        # save reference to avoid lock
        conn = self.connections
        dead_conn = self.dead_connections        
        return (self.resolved_name, self.message_data_sent,
                [(c.id, c.stat_bytes, c.stat_num_msg, not c.done) for c in chain(conn, dead_conn)] )

    def add_subscriber_listener(self, l):
        """
        Add a L{SubscribeListener} for subscribe events.
        @param l: listener instance
        @type  l: L{SubscribeListener}
        """
        self.subscriber_listeners.append(l)
        
    def acquire(self):
        """lock for thread-safe publishing to this transport"""
        if self.publock is not None:
            self.publock.acquire()
        
    def release(self):
        """lock for thread-safe publishing to this transport"""
        if self.publock is not None:
            self.publock.release()
        
    def add_connection(self, c):
        """
        Add a connection to this topic. This must be a PubTransport. If
        the latch is enabled, c will be sent a the value of the
        latch.
        @param c: connection instance
        @type  c: L{Transport}
        @return: True if connection was added
        @rtype: bool
        """
        super(_PublisherImpl, self).add_connection(c)
        def publish_single(data):
            self.publish(data, connection_override=c)
        for l in self.subscriber_listeners:
            l.peer_subscribe(self.resolved_name, self.publish, publish_single)
        if self.is_latch and self.latch is not None:
            with self.publock:
                self.publish(self.latch, connection_override=c)
        return True
            
    def remove_connection(self, c):
        """
        Remove existing connection from this topic.
        @param c: connection instance to remove
        @type  c: L{Transport}
        """
        super(_PublisherImpl, self).remove_connection(c)
        num = len(self.connections)                
        for l in self.subscriber_listeners:
            l.peer_unsubscribe(self.resolved_name, num)
            
    def publish(self, message, connection_override=None):
        """
        Publish the data to the topic. If the topic has no subscribers,
        the method will return without any affect. Access to publish()
        should be locked using acquire() and release() in order to
        ensure proper message publish ordering.

        @param message: message data instance to publish
        @type  message: L{Message}
        @param connection_override: publish to this connection instead of all
        @type  connection_override: L{Transport}
        @return: True if the data was published, False otherwise.
        @rtype: bool
        @raise roslib.message.SerializationError: if L{Message} instance is unable to serialize itself
        @raise rospy.ROSException: if topic has been closed or was closed during publish()
        """
        #TODO: should really just use IOError instead of rospy.ROSException

        if self.closed:
            # during shutdown, the topic can get closed, which creates
            # a race condition with user code testing is_shutdown
            if not is_shutdown():
                raise ROSException("publish() to a closed topic")
            else:
                return
            
        if self.is_latch:
            self.latch = message

        if not self.has_connections():
            #publish() falls through
            return False

        if connection_override is None:
            #copy connections so we can iterate safely
            conns = self.connections
        else:
            conns = [connection_override]

        # #2128 test our buffer. I don't now how this got closed in
        # that case, but we can at least diagnose the problem.
        b = self.buff
        try:
            b.tell()

            # serialize the message
            self.seq += 1 #count messages published to the topic
            serialize_message(b, self.seq, message)

            # send the buffer to all connections
            err_con = []
            data = b.getvalue()

            for c in conns:
                try:
                    if not is_shutdown():
                        c.write_data(data)
                except TransportTerminated, e:
                    logdebug("publisher connection to [%s] terminated, see errorlog for details:\n%s"%(c.endpoint_id, traceback.format_exc()))
                    err_con.append(c)
                except Exception, e:
                    # greater severity level
                    logdebug("publisher connection to [%s] terminated, see errorlog for details:\n%s"%(c.endpoint_id, traceback.format_exc()))
                    err_con.append(c)

            # reset the buffer and update stats
            self.message_data_sent += b.tell() #STATS
            b.seek(0)
            b.truncate(0)
            
        except ValueError:
            # operations on self.buff can fail if topic is closed
            # during publish, which often happens during Ctrl-C.
            # diagnose the error and report accordingly.
            if self.closed:
                if is_shutdown():
                    # we offer no guarantees on publishes that occur
                    # during shutdown, so this is not exceptional.
                    return
                else:
                    # this indicates that user-level code most likely
                    # closed the topic, which is exceptional.
                    raise ROSException("topic was closed during publish()")
            else:
                # unexpected, so re-raise original error
                raise

        # remove any bad connections
        for c in err_con:
            try:
                # connection will callback into remove_connection when
                # we close it
                c.close()
            except:
                pass

#################################################################################
# TOPIC MANAGER/LISTENER

class _TopicManager(object):
    """
    Tracks Topic objects
    See L{get_topic_manager()} for singleton access
    """
    
    def __init__(self):
        """ctor."""
        super(_TopicManager, self).__init__()
        self.pubs = {} #: { topic: _PublisherImpl }
        self.subs = {} #: { topic: _SubscriberImpl }
        self.topics = set() # [str] list of topic names
        self.lock = threading.Condition()
        _logger.info("topicmanager initialized")

    def get_pub_sub_info(self):
        """
        get topic publisher and subscriber connection info for getBusInfo() api
        @return: [bus info stats]
          See getBusInfo() API for more data structure details.
        @rtype: list
        """
        try:
            self.lock.acquire()
            info = []
            for s in chain(self.pubs.itervalues(), self.subs.itervalues()):
                info.extend(s.get_stats_info())
            return info
        finally:
            self.lock.release()
            
    def get_pub_sub_stats(self):
        """
        get topic publisher and subscriber stats for getBusStats() api
        @return: [publisherStats, subscriberStats].
          See getBusStats() API for more data structure details.
        @rtype: list
        """
        try:
            self.lock.acquire()
            return [s.get_stats() for s in self.pubs.itervalues()],\
                   [s.get_stats() for s in self.subs.itervalues()]
        finally:
            self.lock.release()
            
    def remove_all(self):
        """
        Remove all registered publication and subscriptions, closing them on removal
        """
        for t in chain(self.pubs.itervalues(), self.subs.itervalues()):
            t.close()
        self.pubs.clear()
        self.subs.clear()        
        
    def _add(self, ps, map, reg_type):
        """
        Add L{_TopicImpl} instance to map
        @param ps: a pub/sub impl instance
        @type  ps: L{_TopicImpl}
        @param map: { topic: _TopicImpl} map to record instance in
        @type  map: dict
        @param reg_type: L{rospy.registration.Registration.PUB} or L{rospy.registration.Registration.SUB}
        @type  reg_type: str
        """
        resolved_name = ps.resolved_name
        _logger.debug("tm._add: %s, %s, %s", resolved_name, ps.type, reg_type)
        with self.lock:
            map[resolved_name] = ps
            self.topics.add(resolved_name)
            
            # NOTE: this call can take a lengthy amount of time (at
            # least until its reimplemented to use queues)
            get_registration_listeners().notify_added(resolved_name, ps.type, reg_type)

    def _recalculate_topics(self):
        """recalculate self.topics. expensive"""
        self.topics = set([x.resolved_name for x in self.pubs.itervalues()] +
                          [x.resolved_name for x in self.subs.itervalues()])
    
    def _remove(self, ps, map, reg_type):
        """
        Remove L{_TopicImpl} instance from map
        @param ps: a pub/sub impl instance
        @type  ps: L{_TopicImpl}
        @param map: topic->_TopicImpl map to remove instance in
        @type  map: dict
        @param reg_type: L{rospy.registration.Registration.PUB} or L{rospy.registration.Registration.SUB}
        @type  reg_type: str
        """
        resolved_name = ps.resolved_name
        _logger.debug("tm._remove: %s, %s, %s", resolved_name, ps.type, reg_type)
        try:
            self.lock.acquire()
            del map[resolved_name]
            self. _recalculate_topics()
            
            # NOTE: this call can take a lengthy amount of time (at
            # least until its reimplemented to use queues)
            get_registration_listeners().notify_removed(resolved_name, ps.type, reg_type)
        finally:
            self.lock.release()

    def get_impl(self, reg_type, resolved_name):
        """
        Get the L{_TopicImpl} for the specified topic. This is mainly for
        testing purposes. Unlike acquire_impl, it does not alter the
        ref count.
        @param resolved_name: resolved topic name
        @type  resolved_name: str
        @param reg_type: L{rospy.registration.Registration.PUB} or L{rospy.registration.Registration.SUB}
        @type  reg_type: str
        """
        if reg_type == Registration.PUB:
            map = self.pubs
        elif reg_type == Registration.SUB:
            map = self.subs
        else:
            raise TypeError("invalid reg_type: %s"%s)
        return map.get(resolved_name, None)
        
    def acquire_impl(self, reg_type, resolved_name, data_class):
        """
        Acquire a L{_TopicImpl} for the specified topic (create one if it
        doesn't exist).  Every L{Topic} instance has a _TopicImpl that
        actually controls the topic resources so that multiple Topic
        instances use the same underlying connections. 'Acquiring' a
        topic implementation marks that another Topic instance is
        using the TopicImpl.
        
        @param resolved_name: resolved topic name
        @type  resolved_name: str
        
        @param reg_type: L{rospy.registration.Registration.PUB} or L{rospy.registration.Registration.SUB}
        @type  reg_type: str
        
        @param data_class: message class for topic
        @type  data_class: L{Message} class
        """
        if reg_type == Registration.PUB:
            map = self.pubs
            impl_class = _PublisherImpl
        elif reg_type == Registration.SUB:
            map = self.subs
            impl_class = _SubscriberImpl
        else:
            raise TypeError("invalid reg_type: %s"%s)
        try:
            self.lock.acquire()
            impl = map.get(resolved_name, None)            
            if not impl:
                impl = impl_class(resolved_name, data_class)
                self._add(impl, map, reg_type)
            impl.ref_count += 1
            return impl
        finally:
            self.lock.release()

    def release_impl(self, reg_type, resolved_name):
        """
        Release a L_{TopicImpl} for the specified topic.

        Every L{Topic} instance has a _TopicImpl that actually
        controls the topic resources so that multiple Topic instances
        use the same underlying connections. 'Acquiring' a topic
        implementation marks that another Topic instance is using the
        TopicImpl.

        @param resolved_name: resolved topic name
        @type  resolved_name: str
        @param reg_type: L{rospy.registration.Registration.PUB} or L{rospy.registration.Registration.SUB}
        @type  reg_type: str
        """
        if reg_type == Registration.PUB:
            map = self.pubs
        else:
            map = self.subs
        with self.lock:
            impl = map.get(resolved_name, None)
            assert impl is not None, "cannot release topic impl as impl [%s] does not exist"%resolved_name
            impl.ref_count -= 1
            assert impl.ref_count >= 0, "topic impl's reference count has gone below zero"
            if impl.ref_count == 0:
                _logger.debug("topic impl's ref count is zero, deleting topic %s...", resolved_name)
                impl.close()
                self._remove(impl, map, reg_type)
                del impl
                _logger.debug("... done deleting topic %s", resolved_name)

    def get_publisher_impl(self, resolved_name):
        """
        @param resolved_name: resolved topic name
        @type  resolved_name: str
        @return: list of L{_PublisherImpl}s
        @rtype: [L{_PublisherImpl}]
        """
        return self.pubs.get(resolved_name, None)

    def get_subscriber_impl(self, resolved_name):
        """
        @param resolved_name: topic name
        @type  resolved_name: str
        @return: subscriber for the specified topic. 
        @rtype: L{_SubscriberImpl}
        """
        return self.subs.get(resolved_name, None)

    def has_subscription(self, resolved_name):
        """
        @param resolved_name: resolved topic name
        @type  resolved_name: str
        @return: True if manager has subscription for specified topic
        @rtype: bool
        """                
        return resolved_name in self.subs

    def has_publication(self, resolved_name):
        """
        @param resolved_name: resolved topic name
        @type  resolved_name: str
        @return: True if manager has publication for specified topic
        @rtype:  bool
        """
        return resolved_name in self.pubs

    def get_topics(self):
        """
        @return: list of topic names this node subscribes to/publishes
        @rtype: [str]
        """                
        return self.topics
    
    def _get_list(self, map):
        return [[k, v.type] for k, v in map.iteritems()]

    ## @return [[str,str],]: list of topics subscribed to by this node, [ [topic1, topicType1]...[topicN, topicTypeN]]
    def get_subscriptions(self):
        return self._get_list(self.subs)

    ## @return [[str,str],]: list of topics published by this node, [ [topic1, topicType1]...[topicN, topicTypeN]]
    def get_publications(self):
        return self._get_list(self.pubs)

set_topic_manager(_TopicManager())

