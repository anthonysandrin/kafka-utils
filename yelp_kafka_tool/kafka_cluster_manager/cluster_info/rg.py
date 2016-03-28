"""This file incorporates handling partitions over replication-groups
(Availability-zones) in our case.
"""
import logging
from collections import defaultdict

from .util import separate_groups


class ReplicationGroup(object):
    """Represent attributes and functions specific to replication-groups
    (Availability zones) abbreviated as rg.
    """

    log = logging.getLogger(__name__)

    def __init__(self, id, brokers=None):
        self._id = id
        if brokers and not isinstance(brokers, set):
            raise TypeError("brokers has to be a set but type is {}".format(type(brokers)))
        self._brokers = brokers or set()

    @property
    def id(self):
        """Return name of replication-groups."""
        return self._id

    def partitions_sib_info(self, over_loaded_brokers, under_loaded_brokers):
        """Count number of siblings of partitions of over_loaded_brokers
        in brokers of under_loaded_brokers.

        Key-term:
        sibling of partition p: Any partition with same topic as p

        rtype: dict((partition, broker): sibling-count of partition in broker)
        """
        sib_count = defaultdict(dict)
        for source_b in over_loaded_brokers:
            for partition_s in source_b.partitions:
                for dest_b in under_loaded_brokers:
                    sib_count[partition_s][dest_b] = partition_s.count_siblings(
                        dest_b.partitions,
                    )
        return sib_count

    @property
    def brokers(self):
        """Return set of brokers."""
        return self._brokers

    def add_broker(self, broker):
        """Add broker to current broker-list."""
        if broker not in self._brokers:
            self._brokers.add(broker)
        else:
            self.log.warning(
                'Broker {broker_id} already present in '
                'replication-group {rg_id}'.format(
                    broker_id=broker.id,
                    rg_id=self.id,
                )
            )

    @property
    def partitions(self):
        """Evaluate and return set of all partitions in replication-group.
        rtype: list, replicas of partitions can reside in this group
        """
        return [
            partition
            for broker in self._brokers
            for partition in broker.partitions
        ]

    def count_replica(self, partition):
        """Return count of replicas of given partition."""
        return self.partitions.count(partition)

    def move_partition(self, rg_destination, victim_partition):
        """Move partition(victim) from current replication-group to destination
        replication-group.

        Step 1: Evaluate source and destination broker
        Step 2: Move partition from source-broker to destination-broker
        """
        # Select best-fit source and destination brokers for partition
        # Best-fit is based on partition-count and presence/absence of
        # Same topic-partition over brokers
        broker_source, broker_destination = self._select_broker_pair(
            rg_destination,
            victim_partition,
        )
        # Actual-movement of victim-partition
        self.log.debug(
            'Moving partition {p_name} from broker {broker_source} to '
            'replication-group:broker {rg_dest}:{dest_broker}'.format(
                p_name=victim_partition.name,
                broker_source=broker_source.id,
                dest_broker=broker_destination.id,
                rg_dest=rg_destination.id,
            ),
        )
        broker_source.move_partition(victim_partition, broker_destination)

    def _select_broker_pair(self, rg_destination, victim_partition):
        """Select best-fit source and destination brokers based on partition
        count and presence of partition over the broker.

        * Get overloaded and underloaded brokers
        Best-fit Selection Criteria:
        Source broker: Select broker containing the victim-partition with
        maximum partitions.
        Destination broker: NOT containing the victim-partition with minimum
        partitions. If no such broker found, return first broker.

        This helps in ensuring:-
        * Topic-partitions are distributed across brokers.
        * Partition-count is balanced across replication-groups.
        """
        # Get overloaded brokers in source replication-group
        over_loaded_brokers = self._select_over_loaded_brokers(
            victim_partition,
        )
        # Get underloaded brokers in destination replication-group
        under_loaded_brokers = rg_destination.select_under_loaded_brokers(
            victim_partition,
        )
        broker_source = self._elect_source_broker(over_loaded_brokers, victim_partition)
        broker_destination = self._elect_dest_broker(
            under_loaded_brokers,
            victim_partition,
        )
        return broker_source, broker_destination

    def _select_over_loaded_brokers(self, victim_partition):
        """Get over-loaded brokers as sorted broker in partition-count and
        containing victim-partition.
        """
        over_loaded_brokers = [
            broker
            for broker in self._brokers
            if victim_partition in broker.partitions
        ]
        return sorted(
            over_loaded_brokers,
            key=lambda b: len(b.partitions),
            reverse=True,
        )

    def select_under_loaded_brokers(self, victim_partition):
        """Get brokers in ascending sorted order of partition-count
        not containing victim-partition.
        """
        under_loaded_brokers = [
            broker
            for broker in self._brokers
            if victim_partition not in broker.partitions
        ]
        return sorted(under_loaded_brokers, key=lambda b: len(b.partitions))

    def _elect_source_broker(self, over_loaded_brokers, victim_partition):
        """Select first broker from given brokers having victim_partition.

        Note: The broker with maximum siblings of victim-partitions (same topic)
        is selected to reduce topic-partition imbalance.
        """
        broker_topic_partition_cnt = [
            (broker, broker.count_partitions(victim_partition.topic))
            for broker in over_loaded_brokers
        ]
        max_count_pair = max(
            broker_topic_partition_cnt,
            key=lambda ele: ele[1],
        )
        return max_count_pair[0]

    def _elect_dest_broker(self, under_loaded_brokers, victim_partition):
        """Select first broker from under_loaded_brokers preferring not having
        partition of same topic as victim partition.
        """
        # Pick broker having least partitions of the given topic
        broker_topic_partition_cnt = [
            (broker, broker.count_partitions(victim_partition.topic))
            for broker in under_loaded_brokers
        ]
        min_count_pair = min(
            broker_topic_partition_cnt,
            key=lambda ele: ele[1],
        )
        return min_count_pair[0]

    def _extract_decommissioned(self):
        return set([b for b in self.brokers if b.decommissioned])

    # Re-balancing brokers
    def rebalance_brokers(self):
        """Rebalance partition-count across brokers."""
        total_partitions = sum(len(b.partitions) for b in self.brokers)
        blacklist = self._extract_decommissioned()
        active_brokers = [b for b in self.brokers if b not in blacklist]
        # Separate brokers based on partition count
        over_loaded_brokers, under_loaded_brokers = separate_groups(
            active_brokers,
            lambda b: len(b.partitions),
            total_partitions,
        )
        # Decommissioned brokers are considered overloaded until they have
        # no more partitions assigned.
        over_loaded_brokers += [b for b in blacklist if not b.empty()]
        if not over_loaded_brokers and not under_loaded_brokers:
            self.log.info(
                'Brokers of replication-group: %s already balanced for '
                'partition-count.',
                self._id,
            )
            return

        sibling_info = self.partitions_sib_info(
            over_loaded_brokers,
            under_loaded_brokers,
        )
        while under_loaded_brokers and over_loaded_brokers:
            # Get best-fit source-broker, destination-broker and partition
            (broker_source, broker_destination, victim_partition), sibling_info = \
                self._get_target_brokers(over_loaded_brokers, under_loaded_brokers, sibling_info)
            # No valid source or target brokers found
            if broker_source and broker_destination:
                # Move partition
                self.log.debug(
                    'Moving partition {p_name} from broker {broker_source} to '
                    'broker {broker_destination}'
                    .format(
                        p_name=victim_partition.name,
                        broker_source=broker_source.id,
                        broker_destination=broker_destination.id,
                    ),
                )
                broker_source.move_partition(victim_partition, broker_destination)
            else:
                # Brokers are balanced or could not be balanced further
                break
            # Re-evaluate under and over-loaded brokers
            over_loaded_brokers, under_loaded_brokers = separate_groups(
                active_brokers,
                lambda b: len(b.partitions),
                total_partitions,
            )
            # As before add brokers to decommission.
            over_loaded_brokers += [b for b in blacklist if not b.empty()]

    def _get_target_brokers(self, over_loaded_brokers, under_loaded_brokers, sibling_info):
        """Pick best-suitable source-broker, destination-broker and partition to
        balance partition-count over brokers in given replication-group.
        """
        # Sort given brokers to ensure determinism
        over_loaded_brokers = sorted(
            over_loaded_brokers,
            key=lambda b: len(b.partitions),
            reverse=True,
        )
        under_loaded_brokers = sorted(
            under_loaded_brokers,
            key=lambda b: len(b.partitions),
        )
        # pick pair of brokers from source and destination brokers with
        # minimum same-partition-count
        # Set result in format: (source, dest, preferred-partition)
        target = (None, None, None)
        min_sibling_partition_cnt = -1
        for source in over_loaded_brokers:
            for dest in under_loaded_brokers:
                if (len(source.partitions) - len(dest.partitions) > 1 or
                        source.decommissioned):
                    best_fit_partition = source.get_preferred_partition(
                        dest,
                        sibling_info,
                    )
                    # If no eligible partition continue with next broker
                    if best_fit_partition is None:
                        continue
                    sibling_cnt = sibling_info[best_fit_partition][dest]
                    assert(sibling_cnt >= 0)
                    if sibling_cnt < min_sibling_partition_cnt \
                            or min_sibling_partition_cnt == -1:
                        min_sibling_partition_cnt = sibling_cnt
                        target = (source, dest, best_fit_partition)
                        if min_sibling_partition_cnt == 0:
                            # Minimum possible sibling-count
                            break
                else:
                    # If relatively-unbalanced then all brokers in destination
                    # will be thereafter, return from here
                    break
        return target, self.update_sibling_info(sibling_info, target[1], target[2])

    def update_sibling_info(self, sibling_info, broker, partition):
        """Update the sibling-info for all siblings of partition after it has been
        moved to destination broker.
        """
        if partition is not None:
            for sibling in set(partition.topic.partitions):
                try:
                    sibling_info[sibling][broker] += 1
                except KeyError:
                    # If there wasn't any sibling before
                    sibling_info[sibling][broker] = 1
        return sibling_info

    def move_partition_replica(self, under_loaded_rg, eligible_partition):
        """Move partition to under-loaded replication-group if possible."""
        # Evaluate possible source and destination-broker
        source_broker, dest_broker = self._get_eligible_broker_pair(
            under_loaded_rg,
            eligible_partition,
        )
        if source_broker and dest_broker:
            self.log.debug(
                'Moving partition {p_name} from broker {source_broker} to '
                'replication-group:broker {rg_dest}:{dest_broker}'.format(
                    p_name=eligible_partition.name,
                    source_broker=source_broker.id,
                    dest_broker=dest_broker.id,
                    rg_dest=under_loaded_rg.id,
                ),
            )
            # Move partition if eligible brokers found
            source_broker.move_partition(eligible_partition, dest_broker)

    def _get_eligible_broker_pair(self, under_loaded_rg, eligible_partition):
        """Evaluate and return source and destination broker-pair from over-loaded
        and under-loaded replication-group if possible, return None otherwise.

        Return source broker with maximum partitions and destination broker with
        minimum partitions based on following conditions:-
        1) At-least one broker in under-loaded group which does not have
        victim-partition. This is because a broker cannot have duplicate replica.
        2) At-least one broker in over-loaded group which has victim-partition
        """
        under_brokers = filter(
            lambda b: eligible_partition not in b.partitions,
            under_loaded_rg.brokers,
        )
        over_brokers = filter(
            lambda b: eligible_partition in b.partitions,
            self.brokers,
        )

        # Get source and destination broker
        source_broker = max(
            over_brokers,
            key=lambda broker: len(broker.partitions),
        )
        dest_broker = min(
            under_brokers,
            key=lambda broker: len(broker.partitions),
        )
        return (source_broker, dest_broker)
