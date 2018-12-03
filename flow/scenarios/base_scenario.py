"""Contains the base scenario class."""

import logging
import random
import numpy as np
import time
import os
import subprocess
import traceback
from lxml import etree
import xml.etree.ElementTree as ElementTree
from copy import deepcopy

try:
    # Import serializable if rllab is installed
    from rllab.core.serializable import Serializable
except ImportError:
    Serializable = object

from flow.core.params import InitialConfig
from flow.core.traffic_lights import TrafficLights
from flow.core.util import makexml, printxml, ensure_dir

E = etree.Element

# Number of retries on accessing the .net.xml file before giving up
RETRIES_ON_ERROR = 10
# number of seconds to wait before trying to access the .net.xml file again
WAIT_ON_ERROR = 1

VEHICLE_LENGTH = 5  # length of vehicles in the network, in meters


class Scenario(Serializable):
    """Base scenario class.

    Initializes a new scenario. Scenarios are used to specify features of
    a network, including the positions of nodes, properties of the edges
    and junctions connecting these nodes, properties of vehicles and
    traffic lights, and other features as well.

    This class uses network specific features to generate the necessary xml
    files needed to initialize a sumo instance. The methods of this class are
    called by the base scenario class.

    Several network specific features can be acquired from this class via a
    plethora of get methods (see documentation).

    This class can be instantiated once and reused in multiple experiments.
    Note that this function stores all the relevant parameters. The
    generate() function still needs to be called separately.
    """

    def __init__(self,
                 name,
                 vehicles,
                 net_params,
                 initial_config=InitialConfig(),
                 traffic_lights=TrafficLights()):
        """Instantiate the base scenario class.

        Attributes
        ----------
        name : str
            A tag associated with the scenario
        vehicles : Vehicles type
            see flow/core/vehicles.py
        net_params : NetParams type
            see flow/core/params.py
        initial_config : InitialConfig type
            see flow/core/params.py
        traffic_lights : flow.core.traffic_lights.TrafficLights type
            see flow/core/traffic_lights.py
        """
        # Invoke serializable if using rllab
        if Serializable is not object:
            Serializable.quick_init(self, locals())

        self.total_veh_types = []

        self.orig_name = name  # To avoid repeated concatenation upon reset
        self.name = name + time.strftime("_%Y%m%d-%H%M%S") + str(time.time())

        self.vehicles = vehicles
        self.net_params = net_params
        self.initial_config = initial_config
        self.traffic_lights = traffic_lights

        self.net_params = net_params
        self.net_path = os.path.dirname(os.path.abspath(__file__)) \
            + "/debug/net/"
        self.cfg_path = os.path.dirname(os.path.abspath(__file__)) \
            + "/debug/cfg/"
        self.vehicle_ids = []

        ensure_dir("%s" % self.net_path)
        ensure_dir("%s" % self.cfg_path)

        self.nodfn = "%s.nod.xml" % self.name
        self.edgfn = "%s.edg.xml" % self.name
        self.typfn = "%s.typ.xml" % self.name
        self.cfgfn = "%s.netccfg" % self.name
        self.netfn = "%s.net.xml" % self.name
        self.confn = "%s.con.xml" % self.name
        self.roufn = "%s.rou.xml" % self.name
        self.addfn = "%s.add.xml" % self.name
        self.sumfn = "%s.sumo.cfg" % self.name
        self.guifn = "%s.gui.cfg" % self.name

        # create the network configuration files
        self._edges, self._connections = self.generate_net(
            self.net_params, self.traffic_lights)

        # list of edges and internal links (junctions)
        self._edge_list = [
            edge_id for edge_id in self._edges.keys() if edge_id[0] != ":"
        ]
        self._junction_list = list(
            set(self._edges.keys()) - set(self._edge_list))

        # maximum achievable speed on any edge in the network
        self.max_speed = max(
            self.speed_limit(edge) for edge in self.get_edge_list())

        # parameters to be specified under each unique subclass's
        # __init__() function
        self.edgestarts = self.specify_edge_starts()

        # these optional parameters need only be used if "no-internal-links"
        # is set to "false" while calling sumo's netconvert function
        self.internal_edgestarts = self.specify_internal_edge_starts()
        self.intersection_edgestarts = self.specify_intersection_edge_starts()

        # in case the user did not write the intersection edge-starts in
        # internal edge-starts as well (because of redundancy), merge the two
        # together
        self.internal_edgestarts += self.intersection_edgestarts
        seen = set()
        self.internal_edgestarts = \
            [item for item in self.internal_edgestarts
             if item[1] not in seen and not seen.add(item[1])]
        self.internal_edgestarts_dict = dict(self.internal_edgestarts)

        # total_edgestarts and total_edgestarts_dict contain all of the above
        # edges, with the former being ordered by position
        if self.net_params.no_internal_links:
            self.total_edgestarts = self.edgestarts
        else:
            self.total_edgestarts = self.edgestarts + self.internal_edgestarts
        self.total_edgestarts.sort(key=lambda tup: tup[1])

        self.total_edgestarts_dict = dict(self.total_edgestarts)

        # length of the network, or the portion of the network in
        # which cars are meant to be distributed
        # (may be overridden by subclass __init__())
        if not hasattr(self, "length"):
            self.length = sum([
                self.edge_length(edge_id) for edge_id in self.get_edge_list()
            ])

        # generate starting position for vehicles in the network
        kwargs = initial_config.additional_params
        positions, lanes = self.generate_starting_positions(
            num_vehicles=vehicles.num_vehicles,
            **kwargs
        )

        # create the sumo configuration files
        cfg_name = self.generate_cfg(self.net_params, self.traffic_lights)

        shuffle = initial_config.shuffle
        self.make_routes(self, positions, lanes, shuffle)

        # specify the location of the sumo configuration file
        self.cfg = self.cfg_path + cfg_name

    def specify_edge_starts(self):
        """Define edge starts for road sections in the network.

        This is meant to provide some global reference frame for the road
        edges in the network.

        MUST BE implemented in any new scenario subclass.

        Returns
        -------
        edgestarts : list
            list of edge names and starting positions,
            ex: [(edge0, pos0), (edge1, pos1), ...]
        """
        raise NotImplementedError

    def specify_intersection_edge_starts(self):
        """Define edge starts for intersections.

        This is meant to provide some global reference frame for the
        intersections in the network.

        This does not need to be specified if no intersections exist. These
        values can be used to determine the distance of some agent from the
        nearest and/or all intersections.

        Returns
        -------
        intersection_edgestarts : list
            list of intersection names and starting positions,
            ex: [(intersection0, pos0), (intersection1, pos1), ...]
        """
        return []

    def specify_internal_edge_starts(self):
        """Define the edge starts for internal edge nodes.

        This is meant to provide some global reference frame for the internal
        edges in the network.

        These edges are the result of finite-length connections between road
        sections. This methods does not need to be specified if "no-internal-
        links" is set to True in net_params.

        Returns
        -------
        internal_edgestarts : list
            list of internal junction names and starting positions,
            ex: [(internal0, pos0), (internal1, pos1), ...]
        """
        return []

    def get_edge(self, x):
        """Compute an edge and relative position from an absolute position.

        Parameters
        ----------
        x : float
            absolute position in network

        Returns
        -------
        edge position : tup
            1st element: edge name (such as bottom, right, etc.)
            2nd element: relative position on edge
        """
        for (edge, start_pos) in reversed(self.total_edgestarts):
            if x >= start_pos:
                return edge, x - start_pos

    def get_x(self, edge, position):
        """Return the absolute position on the track.

        Parameters
        ----------
        edge : str
            name of the edge
        position : float
            relative position on the edge

        Returns
        -------
        absolute_position : float
            position with respect to some global reference
        """
        # if there was a collision which caused the vehicle to disappear,
        # return an x value of -1001
        if len(edge) == 0:
            return -1001

        if edge[0] == ":":
            try:
                return self.internal_edgestarts_dict[edge] + position
            except KeyError:
                # in case several internal links are being generalized for
                # by a single element (for backwards compatibility)
                edge_name = edge.rsplit("_", 1)[0]
                return self.total_edgestarts_dict.get(edge_name, -1001)
        else:
            return self.total_edgestarts_dict[edge] + position

    def generate_starting_positions(self, num_vehicles=None, **kwargs):
        """Generate starting positions for vehicles in the network.

        Calls all other starting position generating classes.

        Parameters
        ----------
        num_vehicles : int, optional
            number of vehicles to be placed on the network. If no value is
            specified, the value is collected from the vehicles class
        kwargs : dict
            additional arguments that may be updated beyond initial
            configurations, such as modifying the starting position

        Returns
        -------
        startpositions : list of tuple (float, float)
            list of start positions [(edge0, pos0), (edge1, pos1), ...]
        startlanes : list of int
            list of start lanes
        """
        num_vehicles = num_vehicles or self.vehicles.num_vehicles

        if self.initial_config.spacing == "uniform":
            startpositions, startlanes = self.gen_even_start_pos(
                self.initial_config, num_vehicles, **kwargs)
        elif self.initial_config.spacing == "random":
            startpositions, startlanes = self.gen_random_start_pos(
                self.initial_config, num_vehicles, **kwargs)
        elif self.initial_config.spacing == "custom":
            startpositions, startlanes = self.gen_custom_start_pos(
                self.initial_config, num_vehicles, **kwargs)
        else:
            raise ValueError('"spacing" argument in initial_config does not '
                             'contain a valid option')

        return startpositions, startlanes

    def gen_even_start_pos(self, initial_config, num_vehicles, **kwargs):
        """Generate uniformly spaced starting positions.

        If the perturbation term in initial_config is set to some positive
        value, then the start positions are perturbed from a uniformly spaced
        distribution by a gaussian whose std is equal to this perturbation
        term.

        Parameters
        ----------
        initial_config : InitialConfig type
            see flow/core/params.py
        num_vehicles : int
            number of vehicles to be placed on the network
        kwargs : dict
            extra components, usually defined during reset to overwrite initial
            config parameters

        Returns
        -------
        startpositions : list of tuple (float, float)
            list of start positions [(edge0, pos0), (edge1, pos1), ...]
        startlanes : list of int
            list of start lanes
        """
        if isinstance(initial_config.edges_distribution, dict):
            # check that the number of vehicle in edges_distribution matches
            # that of the vehicles class
            keys = list(initial_config.edges_distribution.keys())
            if isinstance(initial_config.edges_distribution[keys[0]], int):
                num_vehicles_e = sum(
                    initial_config.edges_distribution[k]
                    for k in initial_config.edges_distribution)
            else:
                num_vehicles_e = sum(sum(pair[1] for pair in
                                     initial_config.edges_distribution[key])
                                     for key in keys)
            assert num_vehicles == num_vehicles_e, \
                'Number of vehicles in edges_distribution and the Vehicles ' \
                'class do not match: {}, {}'.format(num_vehicles,
                                                    num_vehicles_e)

            # add starting positions and lanes
            edges_distribution = deepcopy(initial_config.edges_distribution)
            startpositions, startlanes = [], []
            for key in edges_distribution:
                # set the edge distribution to only include the next edge
                initial_config.edges_distribution = [key]
                # set the number of vehicles that this edge can carry
                if isinstance(edges_distribution[key], int):
                    num_vehicles = edges_distribution[key]
                else:
                    num_vehicles = sum(pair[1]
                                       for pair in edges_distribution[key])
                    veh_ids = []
                    for pair in edges_distribution[key]:
                        veh_ids += [pair[0] for _ in range(pair[1])]
                    self.total_veh_types.extend(veh_ids)  # TODO: maybe shuffle
                # recursively collect the next starting positions and lanes
                pos, lane = self.gen_even_start_pos(
                    initial_config, num_vehicles, **kwargs)
                startpositions.extend(pos)
                startlanes.extend(lane)
            return startpositions, startlanes

        (x0, min_gap, bunching, lanes_distr, available_length,
         available_edges, initial_config) = \
            self._get_start_pos_util(initial_config, num_vehicles, **kwargs)

        # return an empty list of starting positions and lanes if there are no
        # vehicles to be placed
        if num_vehicles == 0:
            return [], []

        increment = available_length / num_vehicles

        # if not all lanes are equal, then we must ensure that vehicles are in
        # two edges at the same time
        flag = False
        lanes = [self.num_lanes(edge) for edge in self.get_edge_list()]
        if any(lanes[0] != lanes[i] for i in range(1, len(lanes))):
            flag = True

        x = x0
        car_count = 0
        startpositions, startlanes = [], []

        # generate uniform starting positions
        while car_count < num_vehicles:
            # collect the position and lane number of each new vehicle
            pos = self.get_edge(x)

            # ensures that vehicles are not placed in an internal junction
            while pos[0] in dict(self.internal_edgestarts).keys():
                # find the location of the internal edge in total_edgestarts,
                # which has the edges ordered by position
                edges = [tup[0] for tup in self.total_edgestarts]
                indx_edge = next(
                    i for i, edge in enumerate(edges) if edge == pos[0])

                # take the next edge in the list, and place the car at the
                # beginning of this edge
                if indx_edge == len(edges) - 1:
                    next_edge_pos = self.total_edgestarts[0]
                else:
                    next_edge_pos = self.total_edgestarts[indx_edge + 1]

                x = next_edge_pos[1]
                pos = (next_edge_pos[0], 0)

            # ensures that you are in an acceptable edge
            while pos[0] not in available_edges:
                x = (x + self.edge_length(pos[0])) % self.length
                pos = self.get_edge(x)

            # ensure that in variable lane settings vehicles always start a
            # vehicle's length away from the start of the edge. This, however,
            # prevents the spacing to be completely uniform.
            if flag and pos[1] < VEHICLE_LENGTH:
                pos0, pos1 = pos
                pos = (pos0, VEHICLE_LENGTH)
                x += VEHICLE_LENGTH
                increment -= (VEHICLE_LENGTH * self.num_lanes(pos0)) / \
                             (num_vehicles - car_count)

            # place vehicles side-by-side in all available lanes on this edge
            for lane in range(min([self.num_lanes(pos[0]), lanes_distr])):
                car_count += 1
                startpositions.append(pos)
                startlanes.append(lane)

                if car_count == num_vehicles:
                    break

            x = (x + increment + VEHICLE_LENGTH + min_gap) % self.length

        # add a perturbation to each vehicle, while not letting the vehicle
        # leave its current edge
        if initial_config.perturbation > 0:
            for i in range(num_vehicles):
                perturb = np.random.normal(0, initial_config.perturbation)
                edge, pos = startpositions[i]
                pos = max(0, min(self.edge_length(edge), pos + perturb))
                startpositions[i] = (edge, pos)

        return startpositions, startlanes

    def gen_random_start_pos(self, initial_config, num_vehicles, **kwargs):
        """Generate random starting positions.

        Parameters
        ----------
        initial_config : InitialConfig type
            see flow/core/params.py
        num_vehicles : int
            number of vehicles to be placed on the network
        kwargs : dict
            extra components, usually defined during reset to overwrite initial
            config parameters

        Returns
        -------
        startpositions : list of tuple (float, float)
            list of start positions [(edge0, pos0), (edge1, pos1), ...]
        startlanes : list of int
            list of start lanes
        """
        if isinstance(initial_config.edges_distribution, dict):
            # check that the number of vehicle in edges_distribution matches
            # that of the vehicles class
            # num_vehicles_e = sum(initial_config.edges_distribution[k]
            #                      for k in initial_config.edges_distribution)
            # assert num_vehicles == num_vehicles_e, \
            #     'Number of vehicles in edges_distribution and the Vehicles ' \
            #     'class do not match: {}, {}'.format(num_vehicles,
            #                                         num_vehicles_e)

            # add starting positions and lanes
            edges_distribution = deepcopy(initial_config.edges_distribution)
            startpositions, startlanes = [], []
            for key in edges_distribution:
                initial_config.edges_distribution = [key]
                if isinstance(edges_distribution[key], int):
                    # set the number of vehicles that this edge can carry
                    num_vehicles = edges_distribution[key]
                else:
                    if isinstance(edges_distribution[key], int):
                        num_vehicles = edges_distribution[key]
                    else:
                        num_vehicles = sum(pair[1]
                                           for pair in edges_distribution[key])
                        veh_ids = []
                        for pair in edges_distribution[key]:
                            print(pair)
                            veh_ids += [pair[0] for _ in range(pair[1])]
                        self.total_veh_types.extend(veh_ids)  # TODO: maybe shuffle
                # recursively collect the next starting positions and lanes
                pos, lane = self.gen_random_start_pos(
                    initial_config, num_vehicles, **kwargs)
                startpositions.extend(pos)
                startlanes.extend(lane)
            return startpositions, startlanes

        (x0, min_gap, bunching, lanes_distr, available_length,
         available_edges, initial_config) = self._get_start_pos_util(
            initial_config, num_vehicles, **kwargs)

        # extra space a vehicle needs to cover from the start of an edge to be
        # fully in the edge and not risk having a gap with a vehicle behind it
        # that is smaller than min_gap
        efs = min_gap + VEHICLE_LENGTH  # extra front space

        for edge in available_edges:
            available_length -= efs * min([self.num_lanes(edge), lanes_distr])

        # choose random positions for each vehicle
        init_absolute_pos = \
            [random.random() * available_length
             for _ in range(num_vehicles)]

        # sort the positions of vehicles, for simplicity in using
        init_absolute_pos.sort()

        # these positions do not include the length of the vehicle, which need
        # to be added
        for i in range(num_vehicles):
            init_absolute_pos[i] += (VEHICLE_LENGTH + min_gap) * i

        decrement = 0
        edge_indx = 0
        startpositions = []
        startlanes = []
        for i in range(num_vehicles):
            edge_i = available_edges[edge_indx]
            pos_i = (init_absolute_pos[i] - decrement) % (
                    self.edge_length(edge_i) - efs)
            lane_i = int(((init_absolute_pos[i] - decrement) - pos_i) /
                         (self.edge_length(edge_i) - efs))

            pos_i += efs

            while lane_i > min([self.num_lanes(edge_i), lanes_distr]) - 1:
                decrement += min([self.num_lanes(edge_i), lanes_distr]) \
                             * (self.edge_length(edge_i) - efs)
                edge_indx += 1

                edge_i = available_edges[edge_indx]
                pos_i = (init_absolute_pos[i] - decrement) % (
                        self.edge_length(edge_i) - efs)

                lane_i = int(((init_absolute_pos[i] - decrement) - pos_i) /
                             (self.edge_length(edge_i) - efs))

                pos_i += efs

            startpositions.append((edge_i, pos_i))
            startlanes.append(lane_i)

        return startpositions, startlanes

    def gen_custom_start_pos(self, initial_config, num_vehicles, **kwargs):
        """Generate a user defined set of starting positions.

        Parameters
        ----------
        initial_config : InitialConfig type
            see flow/core/params.py
        num_vehicles : int
            number of vehicles to be placed on the network
        kwargs : dict
            extra components, usually defined during reset to overwrite initial
            config parameters

        Returns
        -------
        startpositions : list of tuple (float, float)
            list of start positions [(edge0, pos0), (edge1, pos1), ...]
        startlanes : list of int
            list of start lanes
        """
        raise NotImplementedError

    def _get_start_pos_util(self, initial_config, num_vehicles, **kwargs):
        """Prepare initial_config data for starting position methods.

        Performs some pre-processing to the initial_config and **kwargs terms,
        and returns the necessary values for all starting position generating
        functions.

        Parameters
        ----------
        initial_config : InitialConfig type
            see flow/core/params.py
        num_vehicles : int
            number of vehicles to be placed on the network
        kwargs : dict
            extra components, usually defined during reset to overwrite initial
            config parameters

        Returns
        -------
        x0 : float
            starting position of the first vehicle, in meters
        min_gap : float
            minimum gap between vehicles
        bunching : float
            the amount of space freed up in the network (per lane)
        lanes_distribution : int
            number of lanes the vehicles are supposed to be distributed over
        available_length : float
            total available free space for vehicle to be placed, over all lanes
            within the distributable lanes, in meters
        initial_config : InitialConfig type
            modified version of the initial_config parameter

        Raises
        ------
        ValueError
            If there is not enough space to place all vehicles in the allocated
            space in the network with the specified minimum gap.
        """
        min_gap = max(0, initial_config.min_gap)

        x0 = initial_config.x0
        # changes to x0 in kwargs suggests a switch in between rollouts, and so
        # overwrites anything in initial_config
        if "x0" in kwargs:
            x0 = kwargs["x0"]

        bunching = initial_config.bunching
        # check if requested bunching value is not valid (negative)
        if bunching < 0:
            logging.warning('"bunching" cannot be negative; setting to 0')
            bunching = 0
        # changes to bunching in kwargs suggests a switch in between rollouts,
        #  and so overwrites anything in initial_config
        if "bunching" in kwargs:
            bunching = kwargs["bunching"]

        # compute the lanes distribution (adjust of edge cases)
        if initial_config.edges_distribution == "all":
            max_lane = max(
                [self.num_lanes(edge_id) for edge_id in self.get_edge_list()])
        else:
            max_lane = max([
                self.num_lanes(edge_id)
                for edge_id in initial_config.edges_distribution
            ])

        if initial_config.lanes_distribution > max_lane:
            lanes_distribution = max_lane
        elif initial_config.lanes_distribution < 1:
            logging.warning('"lanes_distribution" is too small; setting to 1')
            lanes_distribution = 1
        else:
            lanes_distribution = initial_config.lanes_distribution

        if initial_config.edges_distribution == "all":
            distribution_length = \
                sum(self.edge_length(edge_id) *
                    min([self.num_lanes(edge_id), lanes_distribution])
                    for edge_id in self.get_edge_list()
                    if self.edge_length(edge_id) > min_gap + VEHICLE_LENGTH)
        else:
            distribution_length = \
                sum(self.edge_length(edge_id) *
                    min(self.num_lanes(edge_id), lanes_distribution)
                    for edge_id in initial_config.edges_distribution
                    if self.edge_length(edge_id) > min_gap + VEHICLE_LENGTH)

        if initial_config.edges_distribution == "all":
            available_edges = [
                edge for edge in self.get_edge_list()
                if self.edge_length(edge) > min_gap + VEHICLE_LENGTH]
        else:
            available_edges = [
                edge for edge in initial_config.edges_distribution
                if self.edge_length(edge) > min_gap + VEHICLE_LENGTH]

        available_length = \
            distribution_length - lanes_distribution * bunching - \
            num_vehicles * (min_gap + VEHICLE_LENGTH)

        if available_length < 0:
            raise ValueError("There is not enough space to place all vehicles "
                             "in the network.")

        return (x0, min_gap, bunching, lanes_distribution, available_length,
                available_edges, initial_config)

    def edge_length(self, edge_id):
        """Return the length of a given edge/junction.

        Return -1001 if edge not found.
        """
        try:
            return self._edges[edge_id]["length"]
        except KeyError:
            print('Error in edge length with key', edge_id)
            return -1001

    def speed_limit(self, edge_id):
        """Return the speed limit of a given edge/junction.

        Return -1001 if edge not found.
        """
        try:
            return self._edges[edge_id]["speed"]
        except KeyError:
            print('Error in speed limit with key', edge_id)
            return -1001

    def num_lanes(self, edge_id):
        """Return the number of lanes of a given edge/junction.

        Return -1001 if edge not found.
        """
        try:
            return self._edges[edge_id]["lanes"]
        except KeyError:
            print('Error in num lanes with key', edge_id)
            return -1001

    def get_edge_list(self):
        """Return the names of all edges in the network."""
        return self._edge_list

    def get_junction_list(self):
        """Return the names of all junctions in the network."""
        return self._junction_list

    def next_edge(self, edge, lane):
        """Return the next edge/lane pair from the given edge/lane.

        These edges may also be internal links (junctions). Returns an empty
        list if there are no edge/lane pairs in front.
        """
        try:
            return self._connections["next"][edge][lane]
        except KeyError:
            return []

    def prev_edge(self, edge, lane):
        """Return the edge/lane pair right before this edge/lane.

        These edges may also be internal links (junctions). Returns an empty
        list if there are no edge/lane pairs behind.
        """
        try:
            return self._connections["prev"][edge][lane]
        except KeyError:
            return []

    def generate_net(self, net_params, traffic_lights):
        """Generate Net files for the transportation network.

        Creates different network configuration files for:

        * nodes: x,y position of points which are connected together to form
          links. The nodes may also be fitted with traffic lights, or can be
          treated as priority or zipper merge regions if they combines several
          lanes or edges together.
        * edges: directed edges combining nodes together. These constitute the
          lanes vehicles will be allowed to drive on.
        * types (optional): parameters used to describe common features amount
          several edges of similar types. If edges are not defined with common
          types, this is not needed.
        * connections (optional): describes how incoming and outgoing edge/lane
          pairs on a specific node as connected. If none is specified, SUMO
          handles these connections by default.

        The above files are then combined to form a .net.xml file describing
        the shape of the traffic network in a form compatible with SUMO.

        Parameters
        ----------
        net_params : flow.core.params.NetParams type
            network-specific parameters. Different networks require different
            net_params; see the separate sub-classes for more information.
        traffic_lights : flow.core.traffic_lights.TrafficLights type
            traffic light information, used to determine which nodes are
            treated as traffic lights

        Returns
        -------
        edges : dict <dict>
            Key = name of the edge
            Elements = length, lanes, speed
        connection_data : dict < dict < list<tup> > >
            Key = name of the arriving edge
                Key = lane index
                Element = list of edge/lane pairs that a vehicle can traverse
                from the arriving edge/lane pairs

        """
        # specify the attributes of the nodes
        nodes = self.specify_nodes(net_params)

        # add traffic lights to the nodes
        for n_id in traffic_lights.get_ids():
            indx = next(i for i, nd in enumerate(nodes) if nd["id"] == n_id)
            nodes[indx]["type"] = "traffic_light"

        # for nodes that have traffic lights that haven't been added
        for node in nodes:
            if node["id"] not in traffic_lights.get_ids() \
                    and node.get("type", None) == "traffic_light":
                traffic_lights.add(node["id"])

        # xml file for nodes; contains nodes for the boundary points with
        # respect to the x and y axes
        x = makexml("nodes", "http://sumo.dlr.de/xsd/nodes_file.xsd")
        for node_attributes in nodes:
            x.append(E("node", **node_attributes))
        printxml(x, self.net_path + self.nodfn)

        # collect the attributes of each edge
        edges = self.specify_edges(net_params)

        # xml file for edges
        x = makexml("edges", "http://sumo.dlr.de/xsd/edges_file.xsd")
        for edge_attributes in edges:
            x.append(E("edge", attrib=edge_attributes))
        printxml(x, self.net_path + self.edgfn)

        # specify the types attributes (default is None)
        types = self.specify_types(net_params)

        # xml file for types: contains the the number of lanes and the speed
        # limit for the lanes
        if types is not None:
            x = makexml("types", "http://sumo.dlr.de/xsd/types_file.xsd")
            for type_attributes in types:
                x.append(E("type", **type_attributes))
            printxml(x, self.net_path + self.typfn)

        # specify the connection attributes (default is None)
        connections = self.specify_connections(net_params)

        # xml for connections: specifies which lanes connect to which in the
        # edges
        if connections is not None:
            x = makexml("connections",
                        "http://sumo.dlr.de/xsd/connections_file.xsd")
            for connection_attributes in connections:
                x.append(E("connection", **connection_attributes))
            printxml(x, self.net_path + self.confn)

        # check whether the user requested no-internal-links (default="true")
        if net_params.no_internal_links:
            no_internal_links = "true"
        else:
            no_internal_links = "false"

        # xml file for configuration, which specifies:
        # - the location of all files of interest for sumo
        # - output net file
        # - processing parameters for no internal links and no turnarounds
        x = makexml("configuration",
                    "http://sumo.dlr.de/xsd/netconvertConfiguration.xsd")
        t = E("input")
        t.append(E("node-files", value=self.nodfn))
        t.append(E("edge-files", value=self.edgfn))
        if types is not None:
            t.append(E("type-files", value=self.typfn))
        if connections is not None:
            t.append(E("connection-files", value=self.confn))
        x.append(t)
        t = E("output")
        t.append(E("output-file", value=self.netfn))
        x.append(t)
        t = E("processing")
        t.append(E("no-internal-links", value="%s" % no_internal_links))
        t.append(E("no-turnarounds", value="true"))
        x.append(t)
        printxml(x, self.net_path + self.cfgfn)

        subprocess.call(
            [
                "netconvert -c " + self.net_path + self.cfgfn +
                " --output-file=" + self.cfg_path + self.netfn +
                ' --no-internal-links="%s"' % no_internal_links
            ],
            shell=True)

        # collect data from the generated network configuration file
        error = None
        for _ in range(RETRIES_ON_ERROR):
            try:
                edges_dict, conn_dict = self._import_edges_from_net()
                return edges_dict, conn_dict
            except Exception:
                print("Error during start: {}".format(traceback.format_exc()))
                print("Retrying in {} seconds...".format(WAIT_ON_ERROR))
                time.sleep(WAIT_ON_ERROR)
        raise error

    def generate_cfg(self, net_params, traffic_lights):
        """Generate .sumo.cfg files using net files and netconvert.

        This includes files such as the routes vehicles can traverse,
        properties of the traffic lights, and the view settings of the gui
        (whether the gui is used or not). The background of the gui is set here
        to be grey, with RGB values: (100, 100, 100).

        Parameters
        ----------
        net_params : NetParams type
            see flow/core/params.py
        traffic_lights : flow.core.traffic_lights.TrafficLights type
            traffic light information, used to determine which nodes are
            treated as traffic lights
        """
        start_time = 0
        end_time = None

        # specify routes vehicles can take
        self.rts = self.specify_routes(net_params)

        add = makexml("additional",
                      "http://sumo.dlr.de/xsd/additional_file.xsd")

        # add the routes to the .add.xml file
        for (edge, route) in self.rts.items():
            add.append(E("route", id="route%s" % edge, edges=" ".join(route)))

        # add (optionally) the traffic light properties to the .add.xml file
        if traffic_lights.num_traffic_lights > 0:
            if traffic_lights.baseline:
                tl_params = traffic_lights.actuated_default()
                tl_type = str(tl_params["tl_type"])
                program_id = str(tl_params["program_id"])
                phases = tl_params["phases"]
                max_gap = str(tl_params["max_gap"])
                detector_gap = str(tl_params["detector_gap"])
                show_detector = tl_params["show_detectors"]

                detectors = {"key": "detector-gap", "value": detector_gap}
                gap = {"key": "max-gap", "value": max_gap}

                if show_detector:
                    show_detector = {"key": "show-detectors", "value": "true"}
                else:
                    show_detector = {"key": "show-detectors", "value": "false"}

                # FIXME(ak): add abstract method
                nodes = self.specify_tll(net_params)
                tll = []
                for node in nodes:
                    tll.append({
                        "id": node['id'],
                        "type": tl_type,
                        "programID": program_id
                    })

                for elem in tll:
                    e = E("tlLogic", **elem)
                    e.append(E("param", **show_detector))
                    e.append(E("param", **gap))
                    e.append(E("param", **detectors))
                    for phase in phases:
                        e.append(E("phase", **phase))
                    add.append(e)

            else:
                tl_properties = traffic_lights.get_properties()
                for node in tl_properties.values():
                    # At this point, we assume that traffic lights are properly
                    # formed. If there are no phases for a static traffic
                    # light, ignore and use default
                    if node["type"] == "static" and not node.get("phases"):
                        continue

                    elem = {
                        "id": str(node["id"]),
                        "type": str(node["type"]),
                        "programID": str(node["programID"])
                    }
                    if node.get("offset"):
                        elem["offset"] = str(node.get("offset"))

                    e = E("tlLogic", **elem)
                    for key, value in node.items():
                        if key == "phases":
                            for phase in node.get("phases"):
                                e.append(E("phase", **phase))
                        else:
                            e.append(
                                E("param", **{
                                    "key": key,
                                    "value": str(value)
                                }))

                    add.append(e)

        printxml(add, self.cfg_path + self.addfn)

        gui = E("viewsettings")
        gui.append(E("scheme", name="real world"))
        gui.append(
            E("background",
              backgroundColor="100,100,100",
              showGrid="0",
              gridXSize="100.00",
              gridYSize="100.00"))
        printxml(gui, self.cfg_path + self.guifn)

        cfg = makexml("configuration",
                      "http://sumo.dlr.de/xsd/sumoConfiguration.xsd")

        logging.debug(self.netfn)

        cfg.append(
            self._inputs(
                self.name,
                net=self.netfn,
                add=self.addfn,
                rou=self.roufn,
                gui=self.guifn))
        t = E("time")
        t.append(E("begin", value=repr(start_time)))
        if end_time:
            t.append(E("end", value=repr(end_time)))
        cfg.append(t)

        printxml(cfg, self.cfg_path + self.sumfn)
        return self.sumfn

    def make_routes(self, scenario, positions, lanes, shuffle):
        """Generate .rou.xml files using net files and netconvert.

        This file specifies the sumo-specific properties of vehicles with
        similar types, as well as the starting positions of vehicles. The
        starting positions, however, may be modified in real-time (e.g. during
        an environment reset).

        Parameters
        ----------
        scenario : Scenario type
            scenario class calling this method. This contains information on
            the properties and initial states of vehicles in the network.
        positions : list of tuple (str, float)
            list of start positions [(edge0, pos0), (edge1, pos1), ...]
        lanes : list of float
            list of start lanes
        shuffle : bool
            specifies whether the vehicle IDs should be shuffled before the
            vehicles are assigned starting positions
        """
        vehicles = scenario.vehicles
        routes = makexml("routes", "http://sumo.dlr.de/xsd/routes_file.xsd")

        # add the types of vehicles to the xml file
        for params in vehicles.types:
            # print(params)
            type_params_str = {
                key: str(params['type_params'][key])
                for key in params["type_params"]
            }
            # print(type_params_str)
            routes.append(E('vType', id=params['veh_id'], **type_params_str))

        vehicle_ids = deepcopy(vehicles.get_ids())

        if shuffle:
            random.shuffle(vehicle_ids)

        # add the initial positions of vehicles to the xml file
        for i, ((edge, pos), lane) in enumerate(zip(positions, lanes)):
            if len(self.total_veh_types) == 0:
                veh_id = vehicle_ids[i]
                veh_type = vehicles.get_state(veh_id, "type")
            else:
                j = next(k for k, veh_id in enumerate(vehicle_ids) if
                         vehicles.get_state(veh_id, "type") ==
                         self.total_veh_types[i])
                veh_id = deepcopy(vehicle_ids[j])
                veh_type = vehicles.get_state(veh_id, "type")
                del vehicle_ids[j]
            print(veh_id, edge, pos, lane)

            type_depart_speed = vehicles.get_initial_speed(veh_id)
            routes.append(
                self._vehicle(
                    veh_type,
                    "route" + edge,
                    depart="0",
                    id=veh_id,
                    color="1,1,1",
                    departSpeed=str(type_depart_speed),
                    departPos=str(pos),
                    departLane=str(lane)))

        # add the in-flows from various edges to the xml file
        if self.net_params.inflows is not None:
            total_inflows = self.net_params.inflows.get()
            for inflow in total_inflows:
                for key in inflow:
                    if not isinstance(inflow[key], str):
                        inflow[key] = repr(inflow[key])
                routes.append(self._flow(**inflow))

        printxml(routes, self.cfg_path + self.roufn)

    def specify_nodes(self, net_params):
        """Specify the attributes of nodes in the network.

        Parameters
        ----------
        net_params : NetParams type
            see flow/core/params.py

        Returns
        -------
        nodes : list of dict

            A list of node attributes (a separate dict for each node). Nodes
            attributes must include:

            * id {string} -- name of the node
            * x {float} -- x coordinate of the node
            * y {float} -- y coordinate of the node

        Other attributes may also be specified. See:
        http://sumo.dlr.de/wiki/Networks/Building_Networks_from_own_XML-descriptions#Node_Descriptions
        """
        raise NotImplementedError

    def specify_edges(self, net_params):
        """Specify the attributes of edges connecting pairs on nodes.

        Parameters
        ----------
        net_params : NetParams type
            see flow/core/params.py

        Returns
        -------
        edges : list of dict

            A list of edges attributes (a separate dict for each edge). Edge
            attributes must include:

            * id {string} -- name of the edge
            * from {string} -- name of node the directed edge starts from
            * to {string} -- name of the node the directed edge ends at

            In addition, the attributes must contain at least one of the
            following:

            * "numLanes" {int} and "speed" {float} -- the number of lanes and
              speed limit of the edge, respectively
            * type {string} -- a type identifier for the edge, which can be
              used if several edges are supposed to possess the same number of
              lanes, speed limits, etc...

        Other attributes may also be specified. See:
        http://sumo.dlr.de/wiki/Networks/Building_Networks_from_own_XML-descriptions#Edge_Descriptions
        """
        raise NotImplementedError

    def specify_types(self, net_params):
        """Specify the attributes of various edge types (if any exist).

        Parameters
        ----------
        net_params: NetParams type
            see flow/core/params.py

        Returns
        -------
        types: list of dict
            A list of type attributes for specific groups of edges. If none are
            specified, no .typ.xml file is created.

        For information on type attributes, see:
        http://sumo.dlr.de/wiki/Networks/Building_Networks_from_own_XML-descriptions#Type_Descriptions
        """
        return None

    def specify_connections(self, net_params):
        """Specify the attributes of connections.

        These attributes are used to describe how any specific node's incoming
        and outgoing edges/lane pairs are connected. If no connections are
        specified, sumo generates default connections.

        Parameters
        ----------
        net_params: NetParams type
            see flow/core/params.py

        Returns
        -------
        connections : list of dict
            A list of connection attributes. If none are specified, no .con.xml
            file is created.

        For information on type attributes, see:
        http://sumo.dlr.de/wiki/Networks/Building_Networks_from_own_XML-descriptions#Connection_Descriptions
        """
        return None

    def specify_routes(self, net_params):
        """Specify the routes vehicles can take starting from any edge.

        The routes are specified as lists of edges the vehicle must traverse,
        with the first edge corresponding to the edge the vehicle begins on.
        Note that the edges must be connected for the route to be valid.

        Currently, only one route is allowed from any given starting edge.

        Parameters
        ----------
        net_params : NetParams type
            see flow/core/params.py

        Returns
        -------
        routes : dict
            Key = name of the starting edge
            Element = list of edges a vehicle starting from this edge must
            traverse.
        """
        raise NotImplementedError

    def _flow(self, name, vtype, route, **kwargs):
        return E("flow", id=name, route=route, type=vtype, **kwargs)

    def _vehicle(self, type, route, departPos, number=0, id=None, **kwargs):
        if not id and not number:
            raise ValueError("Supply either ID or Number")
        if not id:
            id = type + "_" + str(number)
        return E(
            "vehicle",
            type=type,
            id=id,
            route=route,
            departPos=departPos,
            **kwargs)

    def _inputs(self, name, net=None, rou=None, add=None, gui=None):
        inp = E("input")
        if net is not False:
            if net is None:
                inp.append(E("net-file", value="%s.net.xml" % name))
            else:
                inp.append(E("net-file", value=net))
        if rou is not False:
            if rou is None:
                inp.append(E("route-files", value="%s.rou.xml" % name))
            else:
                inp.append(E("route-files", value=rou))
        if add is not False:
            if add is None:
                inp.append(E("additional-files", value="%s.add.xml" % name))
            else:
                inp.append(E("additional-files", value=add))
        if gui is not False:
            if gui is None:
                inp.append(E("gui-settings-file", value="%s.gui.xml" % name))
            else:
                inp.append(E("gui-settings-file", value=gui))
        return inp

    def _import_edges_from_net(self):
        """Import edges from a configuration file.

        This is a utility function for computing edge information. It imports a
        network configuration file, and returns the information on the edges
        and junctions located in the file.

        Returns
        -------
        net_data : dict <dict>
            Key = name of the edge/junction
            Element = lanes, speed, length
        connection_data : dict < dict < dict < list<tup> > > >
            Key = "prev" or "next", indicating coming from or to this
            edge/lane pair
                Key = name of the edge
                    Key = lane index
                    Element = list of edge/lane pairs preceding or following
                    the edge/lane pairs
        """
        # import the .net.xml file containing all edge/type data
        parser = etree.XMLParser(recover=True)
        tree = ElementTree.parse(
            os.path.join(self.cfg_path, self.netfn), parser=parser)

        root = tree.getroot()

        # Collect information on the available types (if any are available).
        # This may be used when specifying some edge data.
        types_data = dict()

        for typ in root.findall('type'):
            type_id = typ.attrib["id"]
            types_data[type_id] = dict()

            if "speed" in typ.attrib:
                types_data[type_id]["speed"] = float(typ.attrib["speed"])
            else:
                types_data[type_id]["speed"] = None

            if "numLanes" in typ.attrib:
                types_data[type_id]["numLanes"] = int(typ.attrib["numLanes"])
            else:
                types_data[type_id]["numLanes"] = None

        net_data = dict()
        next_conn_data = dict()  # forward looking connections
        prev_conn_data = dict()  # backward looking connections

        # collect all information on the edges and junctions
        for edge in root.findall('edge'):
            edge_id = edge.attrib["id"]

            # create a new key for this edge
            net_data[edge_id] = dict()

            # check for speed
            if "speed" in edge:
                net_data[edge_id]["speed"] = float(edge.attrib["speed"])
            else:
                net_data[edge_id]["speed"] = None

            # if the edge has a type parameters, check that type for a
            # speed and parameter if one was not already found
            if "type" in edge.attrib and edge.attrib["type"] in types_data:
                if net_data[edge_id]["speed"] is None:
                    net_data[edge_id]["speed"] = \
                        float(types_data[edge.attrib["type"]]["speed"])

            # collect the length from the lane sub-element in the edge, the
            # number of lanes from the number of lane elements, and if needed,
            # also collect the speed value (assuming it is there)
            net_data[edge_id]["lanes"] = 0
            for i, lane in enumerate(edge):
                net_data[edge_id]["lanes"] += 1
                if i == 0:
                    net_data[edge_id]["length"] = float(lane.attrib["length"])
                    if net_data[edge_id]["speed"] is None \
                            and "speed" in lane.attrib:
                        net_data[edge_id]["speed"] = float(
                            lane.attrib["speed"])

            # if no speed value is present anywhere, set it to some default
            if net_data[edge_id]["speed"] is None:
                net_data[edge_id]["speed"] = 30

        # collect connection data
        for connection in root.findall('connection'):
            from_edge = connection.attrib["from"]
            from_lane = int(connection.attrib["fromLane"])

            if from_edge[0] != ":" and not self.net_params.no_internal_links:
                # if the edge is not an internal links and the network is
                # allowed to have internal links, then get the next edge/lane
                # pair from the "via" element
                via = connection.attrib["via"].rsplit("_", 1)
                to_edge = via[0]
                to_lane = int(via[1])
            else:
                to_edge = connection.attrib["to"]
                to_lane = int(connection.attrib["toLane"])

            if from_edge not in next_conn_data:
                next_conn_data[from_edge] = dict()

            if from_lane not in next_conn_data[from_edge]:
                next_conn_data[from_edge][from_lane] = list()

            if to_edge not in prev_conn_data:
                prev_conn_data[to_edge] = dict()

            if to_lane not in prev_conn_data[to_edge]:
                prev_conn_data[to_edge][to_lane] = list()

            next_conn_data[from_edge][from_lane].append((to_edge, to_lane))
            prev_conn_data[to_edge][to_lane].append((from_edge, from_lane))

        connection_data = {"next": next_conn_data, "prev": prev_conn_data}

        return net_data, connection_data

    def close(self):
        """Close the scenario class.

        Deletes the xml files that were created by the scenario class. This
        is to prevent them from building up in the debug folder.
        """
        os.remove(self.net_path + self.nodfn)
        os.remove(self.net_path + self.edgfn)
        os.remove(self.net_path + self.cfgfn)
        os.remove(self.cfg_path + self.addfn)
        os.remove(self.cfg_path + self.guifn)
        os.remove(self.cfg_path + self.netfn)
        os.remove(self.cfg_path + self.roufn)
        os.remove(self.cfg_path + self.sumfn)

        # the connection file is not always created
        try:
            os.remove(self.net_path + self.confn)
        except OSError:
            pass

        # neither is the type file
        try:
            os.remove(self.net_path + self.typfn)
        except OSError:
            pass

    def __str__(self):
        """Return the name of the scenario and the number of vehicles."""
        return "Scenario " + self.name + " with " + \
               str(self.vehicles.num_vehicles) + " vehicles."
