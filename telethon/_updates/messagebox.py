"""
This module deals with correct handling of updates, including gaps, and knowing when the code
should "get difference" (the set of updates that the client should know by now minus the set
of updates that it actually knows).

Each chat has its own [`Entry`] in the [`MessageBox`] (this `struct` is the "entry point").
At any given time, the message box may be either getting difference for them (entry is in
[`MessageBox::getting_diff_for`]) or not. If not getting difference, a possible gap may be
found for the updates (entry is in [`MessageBox::possible_gaps`]). Otherwise, the entry is
on its happy path.

Gaps are cleared when they are either resolved on their own (by waiting for a short time)
or because we got the difference for the corresponding entry.

While there are entries for which their difference must be fetched,
[`MessageBox::check_deadlines`] will always return [`Instant::now`], since "now" is the time
to get the difference.
"""
import asyncio
import datetime
import time
from dataclasses import dataclass, field
from .._sessions.types import SessionState, ChannelState
from .. import _tl


# Telegram sends `seq` equal to `0` when "it doesn't matter", so we use that value too.
NO_SEQ = 0

# See https://core.telegram.org/method/updates.getChannelDifference.
BOT_CHANNEL_DIFF_LIMIT = 100000
USER_CHANNEL_DIFF_LIMIT = 100

# > It may be useful to wait up to 0.5 seconds
POSSIBLE_GAP_TIMEOUT = 0.5

# After how long without updates the client will "timeout".
#
# When this timeout occurs, the client will attempt to fetch updates by itself, ignoring all the
# updates that arrive in the meantime. After all updates are fetched when this happens, the
# client will resume normal operation, and the timeout will reset.
#
# Documentation recommends 15 minutes without updates (https://core.telegram.org/api/updates).
NO_UPDATES_TIMEOUT = 15 * 60

# Entry "enum".
# Account-wide `pts` includes private conversations (one-to-one) and small group chats.
ENTRY_ACCOUNT = object()
# Account-wide `qts` includes only "secret" one-to-one chats.
ENTRY_SECRET = object()
# Integers will be Channel-specific `pts`, and includes "megagroup", "broadcast" and "supergroup" channels.


def next_updates_deadline():
    return asyncio.get_running_loop().time() + NO_UPDATES_TIMEOUT


class GapError(ValueError):
    pass


# Represents the information needed to correctly handle a specific `tl::enums::Update`.
@dataclass
class PtsInfo:
    pts: int
    pts_count: int
    entry: object

    @classmethod
    def from_update(cls, update):
        pts = getattr(update, 'pts', None)
        if pts:
            pts_count = getattr(update, 'pts_count', None) or 0
            try:
                entry = update.message.peer_id.channel_id
            except AttributeError:
                entry = getattr(update, 'channel_id', None) or ENTRY_ACCOUNT
            return cls(pts=pts, pts_count=pts_count, entry=entry)

        if qts := getattr(update, 'qts', None):
            pts_count = 1 if isinstance(update, _tl.UpdateNewEncryptedMessage) else 0
            return cls(pts=qts, pts_count=pts_count, entry=ENTRY_SECRET)

        return None


# The state of a particular entry in the message box.
@dataclass
class State:
    # Current local persistent timestamp.
    pts: int

    # Next instant when we would get the update difference if no updates arrived before then.
    deadline: float


# > ### Recovering gaps
# > […] Manually obtaining updates is also required in the following situations:
# > • Loss of sync: a gap was found in `seq` / `pts` / `qts` (as described above).
# >   It may be useful to wait up to 0.5 seconds in this situation and abort the sync in case a new update
# >   arrives, that fills the gap.
#
# This is really easy to trigger by spamming messages in a channel (with as little as 3 members works), because
# the updates produced by the RPC request take a while to arrive (whereas the read update comes faster alone).
@dataclass
class PossibleGap:
    deadline: float
    # Pending updates (those with a larger PTS, producing the gap which may later be filled).
    updates: list  # of updates


# Represents a "message box" (event `pts` for a specific entry).
#
# See https://core.telegram.org/api/updates#message-related-event-sequences.
@dataclass
class MessageBox:
    # Map each entry to their current state.
    map: dict = field(default_factory=dict)  # entry -> state

    # Additional fields beyond PTS needed by `ENTRY_ACCOUNT`.
    date: datetime.datetime = datetime.datetime(*time.gmtime(0)[:6]).replace(tzinfo=datetime.timezone.utc)
    seq: int = NO_SEQ

    # Holds the entry with the closest deadline (optimization to avoid recalculating the minimum deadline).
    next_deadline: object = None  # entry

    # Which entries have a gap and may soon trigger a need to get difference.
    #
    # If a gap is found, stores the required information to resolve it (when should it timeout and what updates
    # should be held in case the gap is resolved on its own).
    #
    # Not stored directly in `map` as an optimization (else we would need another way of knowing which entries have
    # a gap in them).
    possible_gaps: dict = field(default_factory=dict)  # entry -> possiblegap

    # For which entries are we currently getting difference.
    getting_diff_for: set = field(default_factory=set)  # entry

    # Temporarily stores which entries should have their update deadline reset.
    # Stored in the message box in order to reuse the allocation.
    reset_deadlines_for: set = field(default_factory=set)  # entry

    # region Creation, querying, and setting base state.

    def load(self, session_state, channel_states):
        """
        Create a [`MessageBox`] from a previously known update state.
        """
        deadline = next_updates_deadline()

        self.map.clear()
        if session_state.pts != NO_SEQ:
            self.map[ENTRY_ACCOUNT] = State(pts=session_state.pts, deadline=deadline)
        if session_state.qts != NO_SEQ:
            self.map[ENTRY_SECRET] = State(pts=session_state.qts, deadline=deadline)
        self.map.update((s.channel_id, State(pts=s.pts, deadline=deadline)) for s in channel_states)

        self.date = datetime.datetime.fromtimestamp(session_state.date).replace(tzinfo=datetime.timezone.utc)
        self.seq = session_state.seq
        self.next_deadline = ENTRY_ACCOUNT

    def session_state(self):
        """
        Return the current state.

        This should be used for persisting the state.
        """
        return dict(
            pts=self.map[ENTRY_ACCOUNT].pts if ENTRY_ACCOUNT in self.map else NO_SEQ,
            qts=self.map[ENTRY_SECRET].pts if ENTRY_SECRET in self.map else NO_SEQ,
            date=int(self.date.timestamp()),
            seq=self.seq,
        ), {id: state.pts for id, state in self.map.items() if isinstance(id, int)}

    def is_empty(self) -> bool:
        """
        Return true if the message box is empty and has no state yet.
        """
        return ENTRY_ACCOUNT not in self.map

    def check_deadlines(self):
        """
        Return the next deadline when receiving updates should timeout.

        If a deadline expired, the corresponding entries will be marked as needing to get its difference.
        While there are entries pending of getting their difference, this method returns the current instant.
        """
        now = asyncio.get_running_loop().time()

        if self.getting_diff_for:
            return now

        deadline = next_updates_deadline()

        # Most of the time there will be zero or one gap in flight so finding the minimum is cheap.
        if self.possible_gaps:
            deadline = min(deadline, *(gap.deadline for gap in self.possible_gaps.values()))
        elif self.next_deadline in self.map:
            deadline = min(deadline, self.map[self.next_deadline].deadline)

        if now > deadline:
            # Check all expired entries and add them to the list that needs getting difference.
            self.getting_diff_for.update(entry for entry, gap in self.possible_gaps.items() if now > gap.deadline)
            self.getting_diff_for.update(entry for entry, state in self.map.items() if now > state.deadline)

            # When extending `getting_diff_for`, it's important to have the moral equivalent of
            # `begin_get_diff` (that is, clear possible gaps if we're now getting difference).
            for entry in self.getting_diff_for:
                self.possible_gaps.pop(entry, None)

        return deadline

    # Reset the deadline for the periods without updates for a given entry.
    #
    # It also updates the next deadline time to reflect the new closest deadline.
    def reset_deadline(self, entry, deadline):
        if entry in self.map:
            self.map[entry].deadline = deadline
            # TODO figure out why not in map may happen

        if self.next_deadline == entry:
            # If the updated deadline was the closest one, recalculate the new minimum.
            self.next_deadline = min(self.map.items(), key=lambda entry_state: entry_state[1].deadline)[0]
        elif self.next_deadline in self.map and deadline < self.map[self.next_deadline].deadline:
            # If the updated deadline is smaller than the next deadline, change the next deadline to be the new one.
            self.next_deadline = entry
        # else an unrelated deadline was updated, so the closest one remains unchanged.

    # Convenience to reset a channel's deadline, with optional timeout.
    def reset_channel_deadline(self, channel_id, timeout):
        self.reset_deadline(channel_id, asyncio.get_running_loop().time() + (timeout or NO_UPDATES_TIMEOUT))

    # Reset all the deadlines in `reset_deadlines_for` and then empty the set.
    def apply_deadlines_reset(self):
        next_deadline = next_updates_deadline()

        reset_deadlines_for = self.reset_deadlines_for
        self.reset_deadlines_for = set()  # "move" the set to avoid self.reset_deadline() from touching it during iter

        for entry in reset_deadlines_for:
            self.reset_deadline(entry, next_deadline)

        reset_deadlines_for.clear()  # reuse allocation, the other empty set was a temporary dummy value
        self.reset_deadlines_for = reset_deadlines_for

    # Sets the update state.
    #
    # Should be called right after login if [`MessageBox::new`] was used, otherwise undesirable
    # updates will be fetched.
    def set_state(self, state):
        deadline = next_updates_deadline()

        if state.pts != NO_SEQ:
            self.map[ENTRY_ACCOUNT] = State(pts=state.pts, deadline=deadline)
        else:
            self.map.pop(ENTRY_ACCOUNT, None)

        if state.qts != NO_SEQ:
            self.map[ENTRY_SECRET] = State(pts=state.qts, deadline=deadline)
        else:
            self.map.pop(ENTRY_SECRET, None)

        self.date = state.date
        self.seq = state.seq

    # Like [`MessageBox::set_state`], but for channels. Useful when getting dialogs.
    #
    # The update state will only be updated if no entry was known previously.
    def try_set_channel_state(self, id, pts):
        if id not in self.map:
            self.map[id] = State(pts=pts, deadline=next_updates_deadline())

    # Begin getting difference for the given entry.
    #
    # Clears any previous gaps.
    def begin_get_diff(self, entry):
        self.getting_diff_for.add(entry)
        self.possible_gaps.pop(entry, None)

    # Finish getting difference for the given entry.
    #
    # It also resets the deadline.
    def end_get_diff(self, entry):
        try:
            self.getting_diff_for.remove(entry)
        except KeyError:
            pass
        self.reset_deadline(entry, next_updates_deadline())
        assert entry not in self.possible_gaps, "gaps shouldn't be created while getting difference"

    # endregion Creation, querying, and setting base state.

    # region "Normal" updates flow (processing and detection of gaps).

    # Process an update and return what should be done with it.
    #
    # Updates corresponding to entries for which their difference is currently being fetched
    # will be ignored. While according to the [updates' documentation]:
    #
    # > Implementations [have] to postpone updates received via the socket while
    # > filling gaps in the event and `Update` sequences, as well as avoid filling
    # > gaps in the same sequence.
    #
    # In practice, these updates should have also been retrieved through getting difference.
    #
    # [updates documentation] https://core.telegram.org/api/updates
    def process_updates(
        self,
        updates,
        chat_hashes,
        result,  # out list of updates; returns list of user, chat, or raise if gap
    ):
        date = getattr(updates, 'date', None)
        if date is None:
            # updatesTooLong is the only one with no date (we treat it as a gap)
            raise GapError

        seq = getattr(updates, 'seq', None) or NO_SEQ
        seq_start = getattr(updates, 'seq_start', None) or seq
        users = getattr(updates, 'users', None) or []
        chats = getattr(updates, 'chats', None) or []
        updates = getattr(updates, 'updates', None) or [updates]

        # > For all the other [not `updates` or `updatesCombined`] `Updates` type constructors
        # > there is no need to check `seq` or change a local state.
        if seq_start != NO_SEQ:
            if self.seq + 1 > seq_start:
                # Skipping updates that were already handled
                return (users, chats)
            elif self.seq + 1 < seq_start:
                # Gap detected
                self.begin_get_diff(ENTRY_ACCOUNT)
                raise GapError
            # else apply

            self.date = date
            if seq != NO_SEQ:
                self.seq = seq

        result.extend(filter(None, (self.apply_pts_info(u, reset_deadline=True) for u in updates)))

        self.apply_deadlines_reset()

        def _sort_gaps(update):
            pts = PtsInfo.from_update(update)
            return pts.pts - pts.pts_count if pts else 0

        if self.possible_gaps:
            # For each update in possible gaps, see if the gap has been resolved already.
            for key in list(self.possible_gaps.keys()):
                self.possible_gaps[key].updates.sort(key=_sort_gaps)

                for _ in range(len(self.possible_gaps[key].updates)):
                    update = self.possible_gaps[key].updates.pop(0)

                    # If this fails to apply, it will get re-inserted at the end.
                    # All should fail, so the order will be preserved (it would've cycled once).
                    update = self.apply_pts_info(update, reset_deadline=False)
                    if update:
                        result.append(update)

            # Clear now-empty gaps.
            self.possible_gaps = {entry: gap for entry, gap in self.possible_gaps.items() if gap.updates}

        return (users, chats)

    # Tries to apply the input update if its `PtsInfo` follows the correct order.
    #
    # If the update can be applied, it is returned; otherwise, the update is stored in a
    # possible gap (unless it was already handled or would be handled through getting
    # difference) and `None` is returned.
    def apply_pts_info(
        self,
        update,
        *,
        reset_deadline,
    ):
        pts = PtsInfo.from_update(update)
        if not pts:
            # No pts means that the update can be applied in any order.
            return update

        # As soon as we receive an update of any form related to messages (has `PtsInfo`),
        # the "no updates" period for that entry is reset.
        #
        # Build the `HashSet` to avoid calling `reset_deadline` more than once for the same entry.
        if reset_deadline:
            self.reset_deadlines_for.add(pts.entry)

        if pts.entry in self.getting_diff_for:
            # Note: early returning here also prevents gap from being inserted (which they should
            # not be while getting difference).
            return None

        if pts.entry in self.map:
            local_pts = self.map[pts.entry].pts
            if local_pts + pts.pts_count > pts.pts:
                # Ignore
                return None
            elif local_pts + pts.pts_count < pts.pts:
                # Possible gap
                # TODO store chats too?
                if pts.entry not in self.possible_gaps:
                    self.possible_gaps[pts.entry] = PossibleGap(
                        deadline=asyncio.get_running_loop().time() + POSSIBLE_GAP_TIMEOUT,
                        updates=[]
                    )

                self.possible_gaps[pts.entry].updates.append(update)
                return None
        else:
            # No previous `pts` known, and because this update has to be "right" (it's the first one) our
            # `local_pts` must be one less.
            local_pts = pts.pts - 1

        # For example, when we're in a channel, we immediately receive:
        # * ReadChannelInbox (pts = X)
        # * NewChannelMessage (pts = X, pts_count = 1)
        #
        # Notice how both `pts` are the same. If we stored the one from the first, then the second one would
        # be considered "already handled" and ignored, which is not desirable. Instead, advance local `pts`
        # by `pts_count` (which is 0 for updates not directly related to messages, like reading inbox).
        if pts.entry in self.map:
            self.map[pts.entry].pts = local_pts + pts.pts_count
        else:
            self.map[pts.entry] = State(pts=local_pts + pts.pts_count, deadline=next_updates_deadline())

        return update

    # endregion "Normal" updates flow (processing and detection of gaps).

    # region Getting and applying account difference.

    # Return the request that needs to be made to get the difference, if any.
    def get_difference(self):
        entry = ENTRY_ACCOUNT
        if entry in self.getting_diff_for:
            if entry in self.map:
                return _tl.fn.updates.GetDifference(
                    pts=self.map[ENTRY_ACCOUNT].pts,
                    pts_total_limit=None,
                    date=self.date,
                    qts=self.map[ENTRY_SECRET].pts if ENTRY_SECRET in self.map else NO_SEQ,
                )
            else:
                # TODO investigate when/why/if this can happen
                self.end_get_diff(entry)

        return None

    # Similar to [`MessageBox::process_updates`], but using the result from getting difference.
    def apply_difference(
        self,
        diff,
        chat_hashes,
    ):
        if isinstance(diff, _tl.updates.DifferenceEmpty):
            self.date = diff.date
            self.seq = diff.seq
            self.end_get_diff(ENTRY_ACCOUNT)
            return [], [], []
        elif isinstance(diff, _tl.updates.Difference):
            self.end_get_diff(ENTRY_ACCOUNT)
            chat_hashes.extend(diff.users, diff.chats)
            return self.apply_difference_type(diff)
        elif isinstance(diff, _tl.updates.DifferenceSlice):
            chat_hashes.extend(diff.users, diff.chats)
            return self.apply_difference_type(diff)
        elif isinstance(diff, _tl.updates.DifferenceTooLong):
            # TODO when are deadlines reset if we update the map??
            self.map[ENTRY_ACCOUNT].pts = diff.pts
            self.end_get_diff(ENTRY_ACCOUNT)
            return [], [], []

    def apply_difference_type(
        self,
        diff,
    ):
        state = getattr(diff, 'intermediate_state', None) or diff.state
        self.set_state(state)

        for u in diff.other_updates:
            if isinstance(u, _tl.UpdateChannelTooLong):
                self.begin_get_diff(u.channel_id)

        diff.other_updates.extend(_tl.UpdateNewMessage(
            message=m,
            pts=NO_SEQ,
            pts_count=NO_SEQ,
        ) for m in diff.new_messages)
        diff.other_updates.extend(_tl.UpdateNewEncryptedMessage(
            message=m,
            qts=NO_SEQ,
        ) for m in diff.new_encrypted_messages)

        return diff.other_updates, diff.users, diff.chats

    # endregion Getting and applying account difference.

    # region Getting and applying channel difference.

    # Return the request that needs to be made to get a channel's difference, if any.
    def get_channel_difference(
        self,
        chat_hashes,
    ):
        entry = next((id for id in self.getting_diff_for if isinstance(id, int)), None)
        if not entry:
            return None

        packed = chat_hashes.get(entry)
        if not packed:
            # Cannot get channel difference as we're missing its hash
            self.end_get_diff(entry)
            # Remove the outdated `pts` entry from the map so that the next update can correct
            # it. Otherwise, it will spam that the access hash is missing.
            self.map.pop(entry, None)
            return None

        state = self.map.get(entry)
        if not state:
            # TODO investigate when/why/if this can happen
            # Cannot get channel difference as we're missing its pts
            self.end_get_diff(entry)
            return None

        return _tl.fn.updates.GetChannelDifference(
            force=False,
            channel=_tl.InputChannel(packed.id, packed.hash),
            filter=_tl.ChannelMessagesFilterEmpty(),
            pts=state.pts,
            limit=BOT_CHANNEL_DIFF_LIMIT if chat_hashes.self_bot else USER_CHANNEL_DIFF_LIMIT
        )

    # Similar to [`MessageBox::process_updates`], but using the result from getting difference.
    def apply_channel_difference(
        self,
        request,
        diff,
        chat_hashes,
    ):
        entry = request.channel.channel_id
        self.possible_gaps.pop(entry, None)

        if isinstance(diff, _tl.updates.ChannelDifferenceEmpty):
            assert diff.final
            self.end_get_diff(entry)
            self.map[entry].pts = diff.pts
            return [], [], []
        elif isinstance(diff, _tl.updates.ChannelDifferenceTooLong):
            assert diff.final
            self.map[entry].pts = diff.dialog.pts
            chat_hashes.extend(diff.users, diff.chats)
            self.reset_channel_deadline(entry, diff.timeout)
            # This `diff` has the "latest messages and corresponding chats", but it would
            # be strange to give the user only partial changes of these when they would
            # expect all updates to be fetched. Instead, nothing is returned.
            return [], [], []
        elif isinstance(diff, _tl.updates.ChannelDifference):
            if diff.final:
                self.end_get_diff(entry)

            self.map[entry].pts = diff.pts
            diff.other_updates.extend(_tl.UpdateNewMessage(
                message=m,
                pts=NO_SEQ,
                pts_count=NO_SEQ,
            ) for m in diff.new_messages)
            chat_hashes.extend(diff.users, diff.chats)
            self.reset_channel_deadline(entry, None)

            return diff.other_updates, diff.users, diff.chats

    # endregion Getting and applying channel difference.
