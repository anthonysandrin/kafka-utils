# -*- coding: utf-8 -*-
# Copyright 2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import logging
import sys

from .cluster_balancer import ClusterBalancer
from .error import BrokerDecommissionError
from .error import EmptyReplicationGroupError
from .error import InvalidBrokerIdError
from .error import InvalidPartitionError
from .error import InvalidReplicationFactorError
from .error import NotEligibleGroupError
from .error import RebalanceError
from .util import compute_optimum
from .util import separate_groups


class PartitionCountBalancer(ClusterBalancer):
    """An implementation of cluster rebalancing that tries to achieve balance
    by considering the number of partitions and leaders on each broker.

    :param cluster_topology: The ClusterTopology object that should be acted
        on.
    :param args: The program arguments.
    """

    def __init__(self, cluster_topology, args):
        super(PartitionCountBalancer, self).__init__(cluster_topology, args)
        self.log = logging.getLogger(self.__class__.__name__)

    def _set_arg_default(self, arg, value):
        if not hasattr(self.args, arg):
            setattr(self.args, arg, value)

    def parse_args(self, balancer_args):
        self._set_arg_default('replication_groups', False)
        self._set_arg_default('brokers', False)
        self._set_arg_default('leaders', False)
        self._set_arg_default('max_partition_movements', None)
        self._set_arg_default('max_movement_size', None)
        self._set_arg_default('max_leader_changes', None)
        parser = argparse.ArgumentParser(
            prog=self.__class__.__name__,
            description='Balance the cluster based on the number of partitions'
            ' per broker and replication-group.',
        )
        parser.parse_args(balancer_args, self.args)

    def decommission_brokers(self, broker_ids):
        """Decommission a list of brokers trying to keep the replication group
        the brokers belong to balanced.

        :param broker_ids: list of string representing valid broker ids in the cluster
        :raises: InvalidBrokerIdError when the id is invalid.
        """
        groups = set()
        for b_id in broker_ids:
            try:
                broker = self.cluster_topology.brokers[b_id]
            except KeyError:
                self.log.error("Invalid broker id %s.", b_id)
                # Raise an error for now. As alternative we may ignore the
                # invalid id and continue with the others.
                raise InvalidBrokerIdError(
                    "Broker id {} does not exist in cluster".format(b_id),
                )
            broker.mark_decommissioned()
            groups.add(broker.replication_group)

        for group in groups:
            self._decommission_brokers_in_group(group)

    def _decommission_brokers_in_group(self, group):
        """Decommission the marked brokers of a group."""
        try:
            group.rebalance_brokers()
        except EmptyReplicationGroupError:
            self.log.warning("No active brokers left in replication group %s", group)
        for broker in group.brokers:
            if broker.decommissioned and not broker.empty():
                # In this case we need to reassign the remaining partitions
                # to other replication groups
                self.log.info(
                    "Broker %s can't be decommissioned within the same "
                    "replication group %s. Moving partitions to other "
                    "replication groups.",
                    broker,
                    broker.replication_group,
                )
                self._force_broker_decommission(broker)
                # Broker should be empty now
                if not broker.empty():
                    # Decommission may be impossible if there are not enough
                    # brokers to redistributed the replicas.
                    self.log.error(
                        "Could not decommission broker %s. "
                        "Partitions %s cannot be reassigned.",
                        broker,
                        broker.partitions,
                    )
                    raise BrokerDecommissionError("Broker decommission failed.")

    def _force_broker_decommission(self, broker):
        available_groups = [
            rg for rg in self.cluster_topology.rgs.itervalues()
            if rg is not broker.replication_group
        ]

        for partition in broker.partitions.copy():  # partitions set changes during loop
            groups = sorted(
                available_groups,
                key=lambda x: x.count_replica(partition),
            )
            for group in groups:
                self.log.debug(
                    "Try to move partition: %s from broker %s to "
                    "replication group %s",
                    partition,
                    broker,
                    broker.replication_group,
                )
                try:
                    group.acquire_partition(partition, broker)
                    break
                except NotEligibleGroupError:
                    pass

    def rebalance(self):
        if self.args.max_movement_size:
            self.log.error(
                '--max-movement-size can not be specified for {balancer}.'
                ' Exiting.'.format(
                    balancer=self.__class__.__name__,
                ),
            )
            sys.exit(1)

        if self.args.replication_groups:
            self.log.info(
                'Re-balancing replica-count over replication groups: %s',
                ', '.join(str(rg) for rg in self.cluster_topology.rgs.keys()),
            )
            self.rebalance_replication_groups()

        if self.args.brokers:
            self.log.info(
                'Re-balancing partition-count across brokers: %s',
                ', '.join(str(e) for e in self.cluster_topology.brokers.keys()),
            )
            self.rebalance_brokers()

        if self.args.leaders:
            self.log.info(
                'Re-balancing leader-count across brokers: %s',
                ', '.join(str(e) for e in self.cluster_topology.brokers.keys()),
            )
            self.rebalance_leaders()

    def rebalance_replication_groups(self):
        """Rebalance partitions over replication groups.

        First step involves rebalancing replica-count for each partition across
        replication-groups.
        Second step involves rebalancing partition-count across replication-groups
        of the cluster.
        """
        # Balance replicas over replication-groups for each partition
        if any(b.inactive for b in self.cluster_topology.brokers.itervalues()):
            self.log.error(
                "Impossible to rebalance replication groups because of inactive "
                "brokers."
            )
            raise RebalanceError(
                "Impossible to rebalance replication groups because of inactive "
                "brokers"
            )

        # Balance partition-count over replication-groups
        self.rebalance_replicas()

        # Balance partition-count over replication-groups
        self._rebalance_groups_partition_cnt()

    # Re-balancing partition count across brokers
    def rebalance_brokers(self):
        """Rebalance partition-count across brokers within each replication-group."""
        for rg in self.cluster_topology.rgs.itervalues():
            rg.rebalance_brokers()

    # Re-balancing leaders
    def rebalance_leaders(self):
        """Re-order brokers in replicas such that, every broker is assigned as
        preferred leader evenly.
        """
        opt_leader_cnt = len(self.cluster_topology.partitions) // len(self.cluster_topology.brokers)
        # Balanced brokers transfer leadership to their under-balanced followers
        self.rebalancing_non_followers(opt_leader_cnt)

    def rebalancing_non_followers(self, opt_cnt):
        """Transfer leadership to any under-balanced followers on the pretext
        that they remain leader-balanced or can be recursively balanced through
        non-followers (followers of other leaders).

        Context:
        Consider a graph G:
        Nodes: Brokers (e.g. b1, b2, b3)
        Edges: From b1 to b2 s.t. b1 is a leader and b2 is its follower
        State of nodes:
            1. Over-balanced/Optimally-balanced: (OB)
                if leadership-count(broker) >= opt-count
            2. Under-balanced (UB) if leadership-count(broker) < opt-count
            leader-balanced: leadership-count(broker) is in [opt-count, opt-count+1]

        Algorithm:
            1. Use Depth-first-search algorithm to find path between
            between some UB-broker to some OB-broker.
            2. If path found, update UB-broker and delete path-edges (skip-partitions).
            3. Continue with step-1 until all possible paths explored.
        """
        under_brokers = filter(
            lambda b: b.count_preferred_replica() < opt_cnt,
            self.cluster_topology.brokers.itervalues(),
        )
        if under_brokers:
            skip_brokers, skip_partitions = [], []
            for broker in under_brokers:
                skip_brokers.append(broker)
                broker.request_leadership(opt_cnt, skip_brokers, skip_partitions)

        over_brokers = filter(
            lambda b: b.count_preferred_replica() > opt_cnt + 1,
            self.cluster_topology.brokers.itervalues(),
        )
        # Any over-balanced brokers tries to donate their leadership to followers
        if over_brokers:
            skip_brokers, used_edges = [], []
            for broker in over_brokers:
                skip_brokers.append(broker)
                broker.donate_leadership(opt_cnt, skip_brokers, used_edges)

    # Re-balancing partition count across brokers
    def _rebalance_groups_partition_cnt(self):
        """Re-balance partition-count across replication-groups.

        Algorithm:
        The key constraint is not to create any replica-count imbalance while
        moving partitions across replication-groups.
        1) Divide replication-groups into over and under loaded groups in terms
           of partition-count.
        2) For each over-loaded replication-group, select eligible partitions
           which can be moved to under-replicated groups. Partitions with greater
           than optimum replica-count for the group have the ability to donate one
           of their replicas without creating replica-count imbalance.
        3) Destination replication-group is selected based on minimum partition-count
           and ability to accept one of the eligible partition-replicas.
        4) Source and destination brokers are selected based on :-
            * their ability to donate and accept extra partition-replica respectively.
            * maximum and minimum partition-counts respectively.
        5) Move partition-replica from source to destination-broker.
        6) Repeat steps 1) to 5) until groups are balanced or cannot be balanced further.
        """
        # Segregate replication-groups based on partition-count
        total_elements = sum(len(rg.partitions) for rg in self.cluster_topology.rgs.itervalues())
        over_loaded_rgs, under_loaded_rgs = separate_groups(
            self.cluster_topology.rgs.values(),
            lambda rg: len(rg.partitions),
            total_elements,
        )
        if over_loaded_rgs and under_loaded_rgs:
            self.cluster_topology.log.info(
                'Over-loaded replication-groups {over_loaded}, under-loaded '
                'replication-groups {under_loaded} based on partition-count'
                .format(
                    over_loaded=[rg.id for rg in over_loaded_rgs],
                    under_loaded=[rg.id for rg in under_loaded_rgs],
                )
            )
        else:
            self.cluster_topology.log.info('Replication-groups are balanced based on partition-count.')
            return

        # Get optimal partition-count per replication-group
        opt_partition_cnt, _ = compute_optimum(
            len(self.cluster_topology.rgs),
            total_elements,
        )
        # Balance replication-groups
        for over_loaded_rg in over_loaded_rgs:
            for under_loaded_rg in under_loaded_rgs:
                # Filter unique partition with replica-count > opt-replica-count
                # in over-loaded-rgs and <= opt-replica-count in under-loaded-rgs
                eligible_partitions = set(filter(
                    lambda partition:
                    over_loaded_rg.count_replica(partition) >
                    len(partition.replicas) // len(self.cluster_topology.rgs) and
                    under_loaded_rg.count_replica(partition) <=
                    len(partition.replicas) // len(self.cluster_topology.rgs),
                    over_loaded_rg.partitions,
                ))
                # Move all possible partitions
                for eligible_partition in eligible_partitions:
                    # The difference of partition-count b/w the over-loaded and under-loaded
                    # replication-groups should be greater than 1 for convergence
                    if len(over_loaded_rg.partitions) - len(under_loaded_rg.partitions) > 1:
                        over_loaded_rg.move_partition_replica(
                            under_loaded_rg,
                            eligible_partition,
                        )
                    else:
                        break
                    # Move to next replication-group if either of the groups got
                    # balanced, otherwise try with next eligible partition
                    if (len(under_loaded_rg.partitions) == opt_partition_cnt or
                            len(over_loaded_rg.partitions) == opt_partition_cnt):
                        break
                if len(over_loaded_rg.partitions) == opt_partition_cnt:
                    # Move to next over-loaded replication-group if balanced
                    break

    def add_replica(self, partition_name, count=1):
        """Increase the replication-factor for a partition.

        The replication-group to add to is determined as follows:
            1. Find all replication-groups that have brokers not already
                replicating the partition.
            2. Of these, find replication-groups that have fewer than the
                average number of replicas for this partition.
            3. Choose the replication-group with the fewest overall partitions.

        :param partition_name: (topic_id, partition_id) of the partition to add
            replicas of.
        :param count: The number of replicas to add.
        :raises InvalidReplicationFactorError when the resulting replication
        factor is greater than the number of brokers in the cluster.
        """
        try:
            partition = self.cluster_topology.partitions[partition_name]
        except KeyError:
            raise InvalidPartitionError(
                "Partition name {name} not found".format(name=partition_name),
            )
        if partition.replication_factor + count > len(self.cluster_topology.brokers):
            raise InvalidReplicationFactorError(
                "Cannot increase replication factor to {0}. There are only "
                "{1} brokers."
                .format(
                    partition.replication_factor + count,
                    len(self.cluster_topology.brokers),
                )
            )

        non_full_rgs = [
            rg
            for rg in self.cluster_topology.rgs.values()
            if rg.count_replica(partition) < len(rg.brokers)
        ]
        for _ in xrange(count):
            total_replicas = sum(
                rg.count_replica(partition)
                for rg in non_full_rgs
            )
            opt_replicas, _ = compute_optimum(
                len(non_full_rgs),
                total_replicas,
            )
            under_replicated_rgs = [
                rg
                for rg in non_full_rgs
                if rg.count_replica(partition) < opt_replicas
            ]
            candidate_rgs = under_replicated_rgs or non_full_rgs
            rg = min(candidate_rgs, key=lambda rg: len(rg.partitions))

            rg.add_replica(partition)

            if rg.count_replica(partition) >= len(rg.brokers):
                non_full_rgs.remove(rg)

    def remove_replica(self, partition_name, osr_broker_ids, count=1):
        """Remove one replica of a partition from the cluster.

        The replication-group to remove from is determined as follows:
            1. Find all replication-groups that contain at least one
                out-of-sync replica for this partition.
            2. Of these, find replication-groups with more than the average
                number of replicas of this partition.
            3. Choose the replication-group with the most overall partitions.
            4. Repeat steps 1-3 with in-sync replicas

        After this operation, the preferred leader for this partition will
        be set to the broker that leads the fewest other partitions, even if
        the current preferred leader is not removed.
        This is done to keep the number of preferred replicas balanced across
        brokers in the cluster.

        :param partition_name: (topic_id, partition_id) of the partition to
            remove replicas of.
        :param osr_broker_ids: A list of the partition's out-of-sync broker ids.
        :param count: The number of replicas to remove.
        :raises: InvalidReplicationFactorError when count is greater than the
        replication factor of the partition.
        """
        try:
            partition = self.cluster_topology.partitions[partition_name]
        except KeyError:
            raise InvalidPartitionError(
                "Partition name {name} not found".format(name=partition_name),
            )
        if partition.replication_factor <= count:
            raise InvalidReplicationFactorError(
                "Cannot remove {0} replicas. Replication factor is only {1}."
                .format(count, partition.replication_factor)
            )

        osr = []
        for broker_id in osr_broker_ids:
            try:
                osr.append(self.cluster_topology.brokers[broker_id])
            except KeyError:
                raise InvalidBrokerIdError(
                    "No broker found with id {bid}".format(bid=broker_id),
                )

        non_empty_rgs = [
            rg
            for rg in self.cluster_topology.rgs.values()
            if rg.count_replica(partition) > 0
        ]
        rgs_with_osr = [
            rg
            for rg in non_empty_rgs
            if any(b in osr for b in rg.brokers)
        ]

        for _ in xrange(count):
            candidate_rgs = rgs_with_osr or non_empty_rgs
            total_replicas = sum(
                rg.count_replica(partition)
                for rg in candidate_rgs
            )
            opt_replica_cnt, _ = compute_optimum(
                len(candidate_rgs),
                total_replicas,
            )
            over_replicated_rgs = [
                rg
                for rg in candidate_rgs
                if rg.count_replica(partition) > opt_replica_cnt
            ]
            candidate_rgs = over_replicated_rgs or candidate_rgs
            rg = max(candidate_rgs, key=lambda rg: len(rg.partitions))

            osr_in_rg = [b for b in rg.brokers if b in osr]
            rg.remove_replica(partition, osr_in_rg)

            osr = [b for b in osr if b in partition.replicas]
            if rg in rgs_with_osr and len(osr_in_rg) == 1:
                rgs_with_osr.remove(rg)
            if rg.count_replica(partition) == 0:
                non_empty_rgs.remove(rg)

        new_leader = min(
            partition.replicas,
            key=lambda broker: broker.count_preferred_replica(),
        )
        partition.swap_leader(new_leader)
