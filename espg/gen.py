#!/usr/bin/env python3

"""gen.py - Fifth stab at a parser generator.

**Grammars.**
A grammar is a dictionary {str: [[symbol]]} mapping names of nonterminals to
lists of right-hand sides. Each right-hand side is a list of symbols. There
are several kinds of symbols; see grammar.py to learn more.

Instead of a list of right-hand sides, the value of a grammar entry may be a
function; see grammar.Apply for details.

**Token streams.**
The user passes to each method an object representing the input sequence.
This object must support two methods:

*   `src.peek()` returns the kind of the next token, or `None` at the end of input.

*   `src.take(kind)` throws an exception if `src.peek() != kind`;
    otherwise, it removes the next token from the input stream and returns it.
    The special case `src.take(None)` checks that the input stream is empty:
    if so, it returns None; if not, it throws.

For very basic needs, see `lexer.LexicalGrammar`.
"""

import collections
import typing
import io
import sys
from .ordered import OrderedSet, OrderedFrozenSet

from .grammar import (Grammar,
                      Production, Some, CallMethod, InitNt,
                      is_concrete_element,
                      Optional, is_optional,
                      Parameterized, ConditionalRhs, Apply, is_apply, Var,
                      LookaheadRule, is_lookahead_rule, lookahead_contains, lookahead_intersect)
from . import emit
from .pgen_runtime import ACCEPT
from .lexer import SyntaxError


# *** Operations on grammars **************************************************

def fix(f, start):
    """Compute a fixed point of `f`, the hard way, starting from `start`."""
    prev, current = start, f(start)
    while current != prev:
        prev, current = current, f(current)
    return current


def empty_nt_set(grammar):
    """Return the set of all nonterminals in `grammar` that can produce the empty string."""
    def step(empties):
        def production_is_empty(nt, p):
            return all(is_lookahead_rule(e)
                       or is_optional(e)
                       or (grammar.is_nt(e) and e in empties)
                       for e in p.body)
        return set(nt
                   for nt, prods in grammar.nonterminals.items()
                   if any(production_is_empty(nt, prod) for prod in prods))

    return fix(step, set())


def check_cycle_free(grammar):
    """Throw an exception if any nonterminal in `grammar` produces itself
    via a cycle of 1 or more productions.
    """
    assert isinstance(grammar, Grammar)
    empties = empty_nt_set(grammar)

    # OK, first find out which nonterminals directly produce which other
    # nonterminals (after possibly erasing some optional/empty nts).
    direct_produces = {}
    for orig in grammar.nonterminals:
        direct_produces[orig] = set()
        for source_production in grammar.nonterminals[orig]:
            for rhs, _r in expand_optional_symbols_in_rhs(source_production.body):
                result = []
                all_possibly_empty_so_far = True
                # If we break out of the following loop, that means it turns
                # out that this production does not produce *any* strings that
                # are just a single nonterminal.
                for e in rhs:
                    if grammar.is_terminal(e):
                        break  # no good, this production contains a terminal
                    elif grammar.is_nt(e):
                        if e in empties:
                            if all_possibly_empty_so_far:
                                result.append(e)
                        else:
                            if not all_possibly_empty_so_far:
                                break # no good, we have 2+ nonterminals that can't be empty
                            all_possibly_empty_so_far = False
                            result = [e]
                    elif is_optional(e):
                        if grammar.is_nt(e.inner):
                            result.append(e.inner)
                    else:
                        assert is_lookahead_rule(e)
                        pass # ignore the restriction - we lose a little precision here
                else:
                    # If we get here, we didn't break, so our results are good!
                    # nt can definitely produce all the nonterminals in result.
                    direct_produces[orig] |= set(result)

    def step(produces):
        return {
            orig: dest | set(b for a in dest for b in produces[a])
            for orig, dest in produces.items()
        }
    produces = fix(step, direct_produces)

    for nt in grammar.nonterminals:
        if nt in produces[nt]:
            raise ValueError("invalid grammar: nonterminal {} can produce itself".format(nt))


def check_lookahead_rules(grammar):
    """Check that no LookaheadRule appears at the end of a production (or before
    elements that can produce the empty string).

    If there are any offending lookahead rules, throw a ValueError.
    """

    check_cycle_free(grammar)
    for nt in grammar.nonterminals:
        for source_production in grammar.nonterminals[nt]:
            for rhs, _r in expand_optional_symbols_in_rhs(source_production.body):
                # XXX BUG: The next if-condition is insufficient, since it
                # fails to detect a lookahead restriction followed by a
                # nonterminal that can match the empty string.
                if rhs and is_lookahead_rule(rhs[-1]):
                    raise ValueError("invalid grammar: lookahead restriction at end of production: " +
                                     grammar.production_to_str(nt, source_production.body))


def expand_function_nonterminals(grammar):
    """Replace function nonterminals with production lists.

    Also replaces Apply elements with nt elements and eliminates Var and
    ConditionalRhs objects from the grammar.
    """

    assigned_names = {(goal, None): goal for goal in grammar.goals()}
    todo = collections.deque(assigned_names.keys())
    result = {nt: None for nt in grammar.nonterminals}  # maybe could start empty now

    def get_derived_name(nt, args):
        name = assigned_names.get((nt, args))
        if name is None:
            if args is None:
                name = nt
            else:
                name = Apply(nt, args)
            assigned_names[nt, args] = name
            todo.append((nt, args))
            result[name] = None  # maybe unnecessary now
        return name

    def expand(nt, args):
        """ Return an rhs list, the expansion of grammar.nonterminals[nt](**args). """

        if args is None:
            args_dict = None
        else:
            args_dict = dict(args)

        def evaluate_arg(arg):
            if isinstance(arg, Var):
                return args_dict[arg.name]
            else:
                return arg

        def expand_element(e):
            if grammar.is_nt(e):
                return get_derived_name(e, None)
            elif is_optional(e):
                return Optional(expand_element(e.inner))
            elif is_apply(e):
                return get_derived_name(e.nt, tuple((name, evaluate_arg(arg))
                                                    for name, arg in e.args))
            else:
                return e

        def expand_production(p):
            return p.with_body([expand_element(e) for e in p.body])

        def expand_productions(plist):
            result = []
            for p in plist:
                if isinstance(p, ConditionalRhs):
                    if args_dict[p.param] == p.value:
                        result.append(expand_production(p.rhs))
                else:
                    result.append(expand_production(p))
            return result

        if args is None:
            return expand_productions(grammar.nonterminals[nt])
        else:
            fn = grammar.nonterminals[nt]
            assert len(args) == len(fn.params)
            args = tuple(zip(fn.params, args)) # create activation environment! are we having fun yet
            return expand_productions(fn.body)

    while todo:
        nt, args = todo.popleft()
        name = assigned_names[nt, args]
        if result[name] is None:  # not already expanded
            result[name] = expand(nt, args)
    unreachable_keys = [nt for nt, rhs_list in result.items() if rhs_list is None]
    for key in unreachable_keys:
        del result[key]
    return grammar.with_nonterminals(result)


# *** Start sets and follow sets **********************************************

EMPTY = "(empty)"
END = None


def start_sets(grammar):
    """Compute the start sets for nonterminals in a grammar.

    A nonterminal's start set is the set of tokens that a match for that
    nonterminal may start with, plus EMPTY if it can match the empty string.
    """

    # How this works: Note that we can replace the words "match" and "start
    # with" in the definition above with more queries about start sets.
    #
    # 1.  A nonterminal's start set contains a terminal `t` if any of its
    #     productions contains either `t` or a nonterminal with `t` in *its*
    #     start set, preceded only by zero or more nonterminals that have EMPTY
    #     in *their* start sets. Plus:
    #
    # 2.  A nonterminal's start set contains EMPTY if any of its productions
    #     consists entirely of nonterminals that have EMPTY in *their* start
    #     sets.
    #
    # This definition is rather circular. We want the smallest collection of
    # start sets satisfying these rules, and we get that by iterating to a
    # fixed point.

    start = {nt: OrderedFrozenSet() for nt in grammar.nonterminals}
    done = False
    while not done:
        done = True
        for nt, plist in grammar.nonterminals.items():
            # Compute start set for each `prod` based on `start` so far.
            # Could be incomplete, but we'll ratchet up as we iterate.
            nt_start = OrderedFrozenSet(t for p in plist for t in seq_start(grammar, start, p.body))
            if nt_start != start[nt]:
                start[nt] = nt_start
                done = False
    return start


def seq_start(grammar, start, seq):
    """Compute the start set for a sequence of elements."""
    s = OrderedSet([EMPTY])
    for i, e in enumerate(seq):
        if EMPTY not in s:  # preceding elements never match the empty string
            break
        s.remove(EMPTY)
        if grammar.is_terminal(e):
            s.add(e)
        elif grammar.is_nt(e):
            s |= start[e]
        else:
            assert is_lookahead_rule(e)
            future = seq_start(grammar, start, seq[i + 1:])
            if e.positive:
                future &= e.set
            else:
                future -= e.set
            return OrderedFrozenSet(future)
    return OrderedFrozenSet(s)


def make_start_set_cache(grammar, prods, start):
    """Compute start sets for all suffixes of productions in the grammar.

    Returns a list of lists `cache` such that
    `cache[n][i] == seq_start(grammar, start, prods[n][i:])`.

    (The cache is for speed, since seq_start was being called millions of times.)
    """

    def suffix_start_list(rhs):
        sets = [OrderedFrozenSet([EMPTY])]
        for e in reversed(rhs):
            if grammar.is_terminal(e):
                s = OrderedFrozenSet([e])
            elif grammar.is_nt(e):
                s = start[e]
                if EMPTY in s:
                    s = OrderedFrozenSet((s - {EMPTY}) | sets[-1])
            else:
                assert is_lookahead_rule(e)
                if e.positive:
                    s = OrderedFrozenSet(sets[-1] & e.set)
                else:
                    s = OrderedFrozenSet(sets[-1] - e.set)
            assert isinstance(s, OrderedFrozenSet)
            assert s == seq_start(grammar, start, rhs[len(rhs) - len(sets):])
            sets.append(s)
        sets.reverse()
        assert sets == [seq_start(grammar, start, rhs[i:]) for i in range(len(rhs) + 1)]
        return sets

    return [suffix_start_list(prod.rhs) for prod in prods]


def follow_sets(grammar, prods_with_indexes_by_nt, start_set_cache):
    """Compute all follow sets for nonterminals in a grammar.

    The follow set for a nonterminal `A`, as defined in the book, is "the set
    of terminals that can appear immediately to the right of `A` in some
    sentential form"; plus, "If `A` can be the rightmost symbol in some
    sentential form, then $ is in FOLLOW(A)."

    The `init_nts` argument is necessary to specify what a sentential form is,
    since sentential forms are partial derivations of a particular goal
    nonterminal.

    Returns a default-dictionary mapping nts to follow sets.
    """

    # Set of nonterminals already seen, including those we are in the middle of
    # analyzing. The algorithm starts at `goal` and walks all reachable
    # nonterminals, recursively.
    visited = set()

    # The results. By definition, nonterminals that are not reachable from the
    # goal nt have empty follow sets.
    follow = collections.defaultdict(OrderedSet)

    # If `(x, y) in subsumes_relation`, then x can appear at the end of a
    # production of y, and therefore follow[x] should be <= follow[y].
    # (We could maintain that invariant throughout, but at present we
    # brute-force iterate to a fixed point at the end.)
    subsumes_relation = OrderedSet()

    # `END` is $. It is, of course, in follow[each goal nonterminal]. It gets
    # into other nonterminals' follow sets through the subsumes relation.
    for init_nt in grammar.init_nts:
        follow[init_nt].add(END)

    def visit(nt):
        if nt in visited:
            return
        visited.add(nt)
        for prod_index, rhs in prods_with_indexes_by_nt[nt]:
            for i, symbol in enumerate(rhs):
                if grammar.is_nt(symbol):
                    visit(symbol)
                    after = start_set_cache[prod_index][i + 1]
                    if EMPTY in after:
                        after -= {EMPTY}
                        subsumes_relation.add((symbol, nt))
                    follow[symbol] |= after

    for nt in grammar.init_nts:
        visit(nt)

    # Now iterate to a fixed point on the subsumes relation.
    done = False
    while not done:
        done = True # optimistically
        for target, source in subsumes_relation:
            if follow[source] - follow[target]:
                follow[target] |= follow[source]
                done = False

    return follow


# *** Lowering ****************************************************************

# At this point, lowered productions start getting farther from the original
# source.  We need to associate them with the original grammar in order to
# produce correct output, so we use Prod values to represent productions.
#
# -   `nt` is the name of the nonterminal as it appears in the original grammar.
# -   `index` is the index of the source production, within nt's productions,
#     in the original grammar.
# -   `rhs` is the fully lowered/expanded right-hand-side of the production.
# -   `removals` is the list of indexes of elements in the original rhs
#     which were optional and are not present in this production.
#
# There may be many productions in a grammar that all have the same `nt` and `index`
# because they were all produced from the same source production.
Prod = collections.namedtuple("Prod", "nt index rhs removals action")


def expand_optional_symbols_in_rhs(rhs, start_index=0):
    """Expand a sequence that may contain optional symbols into sequences that don't.

    rhs is a list of symbols, possibly containing optional elements. This
    yields every list that can be made by replacing each optional element
    either with its .inner value, or with nothing.

    Each list is accompanied by the list of the indices of optional elements in
    `rhs` that were dropped.

    For example, `expand_optional_symbols_in_rhs(["if", Optional("else")])`
    yields the two pairs `(["if"], [1])` and `["if", "else"], []`.
    """

    for i in range(start_index, len(rhs)):
        if is_optional(rhs[i]):
            break
    else:
        yield rhs[start_index:], []
        return

    for expanded, r in expand_optional_symbols_in_rhs(rhs, i + 1):
        # without rhs[i]
        yield rhs[start_index:i] + expanded, [i] + r
        # with rhs[i]
        yield rhs[start_index:i] + [rhs[i].inner] + expanded, r


def expand_all_optional_elements(grammar):
    """Expand optional elements in the grammar.

    We replace each production that contains an optional element with two
    productions: one with and one without. Downstream of this step, we can
    ignore the possibility of optional elements.
    """
    expanded_grammar = {}

    # Put all the productions in one big list, so each one has an index.
    # We will use the indices in the action table (as arguments to Reduce actions).
    prods = []
    prods_with_indexes_by_nt = collections.defaultdict(list)

    for nt in grammar.nonterminals:
        expanded_grammar[nt] = []
        for prod_index, p in enumerate(grammar.nonterminals[nt]):
            for expanded_rhs, removals in expand_optional_symbols_in_rhs(p.body):
                def adjust_reduce_expr(expr):
                    if isinstance(expr, int):
                        if expr in removals:
                            return None
                        was_optional = is_optional(p.body[expr])
                        expr -= sum(1 for r in removals if r < expr)
                        if was_optional:
                            return Some(expr)
                        else:
                            return expr
                    elif expr is None:
                        return None
                    elif isinstance(expr, Some):
                        return Some(adjust_reduce_expr(expr.inner))
                    elif isinstance(expr, CallMethod):
                        return CallMethod(expr.method, [adjust_reduce_expr(arg)
                                                        for arg in expr.args])
                    elif expr == 'accept':
                        # doesn't need to be adjusted because 'accept' isn't
                        # turned into code downstream.
                        return 'accept'
                    else:
                        raise TypeError("internal error: unrecognized element {!r}".format(expr))

                adjusted_action = adjust_reduce_expr(p.action)
                expanded_grammar[nt].append(
                    Production(nt=p.nt, body=expanded_rhs, action=adjusted_action))
                prods.append(Prod(nt, prod_index, expanded_rhs, removals, adjusted_action))
                prods_with_indexes_by_nt[nt].append((len(prods) - 1, expanded_rhs))

    return grammar.with_nonterminals(expanded_grammar), prods, prods_with_indexes_by_nt


def make_epsilon_free_step_1(grammar):
    """ Return a clone of `grammar` in which all uses of nonterminals
    that match the empty string are wrapped in Optional.

    `grammar` must already be cycle-free.
    """

    empties = empty_nt_set(grammar)

    def hack(e):
        if grammar.is_nt(e) and e in empties:
            return Optional(e)
        return e

    return grammar.with_nonterminals({
        nt: [p.with_body(map(hack, p.body)) for p in plist]
        for nt, plist in grammar.nonterminals.items()
    })


def make_epsilon_free_step_2(grammar):
    """Return a clone of `grammar` with empty right-hand sides removed.

    All empty productions are removed except any for the goal nonterminals,
    so the grammar still recognizes the same language.
    """
    goal_nts = set(grammar.goals())
    return grammar.with_nonterminals({
        nt: [p for p in plist if len(p.body) > 0 or nt in goal_nts]
        for nt, plist in grammar.nonterminals.items()
    })


# *** The path algorithm ******************************************************

def find_path(start_set, successors, test):
    """Find a path from a value in `start_set` to a value that passes `test`.

    `start_set` is an iterable of "points". `successors` is a function mapping
    a point to an iterable of (edge, point) pairs. `test` is a predicate on
    points.  All points must support hashing; edges can be any value.

    Returns the shortest list `path` such that:
    - `path[0] in start_set`;
    - for every triplet `a, e, b` of adjacent elements in `path`
      starting with an even index, `(e, b) in successors(a)`;
    - `test(path[-1])`.

    If no such path exists, returns None.

    """

    # This implementation is long! I was tired when I wrote it.

    # Get started.
    links = {}
    todo = collections.deque()
    for p in start_set:
        if p not in links:
            links[p] = None
            if test(p):
                return [p]
            todo.append(p)

    # Iterate.
    found = False
    while todo:
        a = todo.popleft()
        for edge, b in successors(a):
            if b not in links:
                links[b] = a, edge
                if test(b):
                    found = True
                    todo.clear()
                    break
                todo.append(b)
    if not found:
        return None

    # Reconstruct how we got here.
    path = [b]
    while links[b] is not None:
        a, edge = links[b]
        path.append(edge)
        path.append(a)
        b = a
    path.reverse()
    return path


# *** Parser generation *******************************************************

# ## LR parsers: Why?
#
# Consider a single production `expr ::= expr "+" term` being parsed in a
# recursive descent parser. As we read the source left to right, our parser's
# internal state looks like this (marking our place with a dot):
#
#     expr ::= · expr "+" term
#     expr ::= expr · "+" term
#     expr ::= expr "+" · term
#     expr ::= expr "+" term ·
#
# As we go, we build an AST. First we parse an *expr* and temporarily set it
# aside. Then we expect to see a `+` operator. Then we parse a *term*. Then,
# having got to the end, we create an AST node for the whole addition
# expression.
#
# Since the grammar is nested, at run time we really have a stack of these
# intermediate states.
#
# But how do we decide which production we should be matching? Often the first
# token just tells us: the `while` keyword means there's a `while` statement
# coming up. Grammars in which this is always the case are called LL(1). But
# while it's possible to wrangle *most* of the ES grammar into an LL(1) form,
# not everything works out. For example, here's the ES assignment syntax (much
# simplified):
#
#     assignment ::= sum
#     assignment ::= primitive "=" assignment
#     sum ::= primitive
#     sum ::= sum "+" primitive
#     primitive ::= VAR
#
# Note that the bogus assignment `a + b = c` doesn't parse because `a + b`
# isn't a primitive.
#
# Suppose we want to parse an expression, and the first token is `a`. We don't
# know yet which *assignment* production to use. So this grammar is not in
# LL(1).
#
#
# ## LR parsers: How
#
# An LR parser generator allows for a *superposition* of states. While parsing,
# we can sometimes have multiple productions at once that might match. It's
# like how in quantum theory, Schrödinger’s cat can tentatively be both alive
# and dead until decisive information is observed.
#
# As we read `a = b + c`, our parser's internal state is like this
# (eliding a few steps, like how we recognize that `a` is a primitive):
#
#     current point in input  superposed parser state
#     ----------------------  -----------------------
#     · a = b + c             assignment ::= · sum
#                             assignment ::= · primitive "=" assignment
#
#       (Then, after recognizing that `a` is a *primitive*...)
#
#     a · = b + c             sum ::= primitive ·
#                             assignment ::= primitive · "=" assignment
#
#       (The next token, `=`, rules out the first alternative,
#       collapsing the waveform...)
#
#     a = · b + c             assignment ::= primitive "=" · assignment
#
#       (After recognizing that `b` is a primitive, we again have options:)
#
#     a = b · + c             sum ::= primitive ·
#                             assignment ::= primitive · "=" assignment
#
# And so on. We call each dotted production an "LR item", and the superposition
# of several LR items is called a "state".  (It is not meant to be clear yet
# just *how* the parser knows which rules might match.)
#
# Since the grammar is nested, at run time we'll have a stack of these parser
# state superpositions.
#
# The uncertainty in LR parsing means that code for an LR parser written by
# hand, in the style of recursive descent, would read like gibberish. What we
# can do instead is generate a parser table.


# An LRItem is a snapshot of progress through a single specific production.
#
# *   `prod_index` identifies the production. (Every production in the grammar
#     gets a unique index; see the loop that computes
#     prods_with_indexes_by_nt.)
#
# *   `offset` is the position of the cursor within the production.
#
# `lookahead` and `followed_by` are two totally different kinds of lookahead.
#
# *   `lookahead` is the LookaheadRule, if any, that applies to the immediately
#     upcoming input. It is present only if this LRItem is subject to a
#     `[lookahead]` restriction; otherwise it's None. These restrictions can't
#     extend beyond the end of a production, or else the grammar is invalid.
#     This implements the lookahead restrictions in the ECMAScript grammar.
#     It is not part of any account of LR I've seen.
#
# *   `followed_by` is a completely different kind of lookahead restriction.
#     This is the kind of lookahead that is a central part of canonical LR
#     table generation.  It applies to the token *after* the whole current
#     production, so `followed_by` always applies to completely different and
#     later tokens than `lookahead`.  `followed_by` is a set of terminals; if
#     `None` is in this set, it means `END`, not that the LRItem is
#     unrestricted.
#
LRItem = collections.namedtuple("LRItem", "prod_index offset lookahead followed_by")


def assert_items_are_compatible(grammar, prods, items):
    """Assert that no two elements of `items` have conflicting history.

    All items in the same state must be produced by the same history,
    the same sequence of terminals and nonterminals.
    """
    def item_history(item):
        return [e for e in prods[item.prod_index].rhs[:item.offset] if not is_lookahead_rule(e)]

    pairs = [(item, item_history(item)) for item in items]
    max_item, known_history = max(pairs, key=lambda pair: len(pair[1]))
    for item, history in pairs:
        assert history[:item.offset] == known_history[-item.offset:], \
            "incompatible LR items:\n    {}\n    {}\n".format(
                grammar.lr_item_to_str(prods, max_item),
                grammar.lr_item_to_str(prods, item))


class PgenContext:
    """ The immutable part of the parser generator's data. """
    def __init__(self, grammar, prods, prods_with_indexes_by_nt, start_set_cache, follow):
        self.grammar = grammar
        self.prods = prods
        self.prods_with_indexes_by_nt = prods_with_indexes_by_nt
        self.start_set_cache = start_set_cache
        self.follow = follow

    def make_lr_item(self, *args, **kwargs):
        """Create an LRItem tuple and advance it past any lookahead rules.

        The main algorithm assumes that the "next element" in any LRItem is
        never a lookahead rule. We ensure that is true by processing lookahead
        elements before the LRItem is even exposed.

        We don't bother doing extra work here to eliminate lookahead
        restrictions that are redundant with what's coming up next in the
        grammar, like `[lookahead != NUM]` when the production is
        `name ::= IDENT`. We also don't eliminate items that can't match,
        like `name ::= IDENT` when we have `[lookahead not in {IDENT}]`.

        Such silly items can exist; but we would only care if it caused
        get_state_index to treat equivalent states as distinct. I haven't seen
        that happen for any grammar yet.
        """

        grammar = self.grammar
        prods = self.prods

        item = LRItem(*args, **kwargs)
        assert isinstance(item.followed_by, OrderedFrozenSet)
        rhs = prods[item.prod_index].rhs
        while item.offset < len(rhs) and is_lookahead_rule(rhs[item.offset]):
            item = item._replace(offset=item.offset + 1,
                                 lookahead=lookahead_intersect(item.lookahead, rhs[item.offset]))

        #if item.lookahead is not None:
        if False:  # this block is disabled for now; see comment
            # We want equivalent items to be ==, so the following code
            # canonicalizes lookahead rules, eliminates lookahead rules that
            # are redundant with the upcoming symbols in the rhs, and
            # eliminates items that (due to lookahead rules) won't match
            # anything.
            #
            # This sounds good in theory, and it does reduce the number of
            # LRItems we end up tracking, but I have not found an example where
            # it reduces the number of parser states. So this code is disabled
            # for now.

            expected = self.start_set_cache[item.prod_index][item.offset]
            if item.lookahead.positive:
                ok_set = expected & item.lookahead.set
            else:
                ok_set = expected - item.lookahead.set

            if len(ok_set) == 0:
                return None  # this item can't match anything
            elif ok_set == expected:
                look = None
            else:
                look = LookaheadRule(OrderedFrozenSet(ok_set), True)
            item = item._replace(lookahead=look)
        return item

    def raise_reduce_reduce_conflict(self, state, t, i, j):
        scenario_str = state.traceback()
        p1 = self.prods[i]
        p2 = self.prods[j]

        raise ValueError(
            "reduce-reduce conflict when looking at {} followed by {}\n"
            "can't decide whether to reduce with:\n"
            "    {}\n"
            "or with:\n"
            "    {}\n"
            .format(scenario_str, self.grammar.element_to_str(t),
                    self.grammar.production_to_str(p1.nt, p1.rhs),
                    self.grammar.production_to_str(p2.nt, p2.rhs)))

    def why_start(self, t, prod_index, offset):
        """ Yield a sequence of productions showing why `t in START(prods[prod_index][offset:])`.

        If `prods[prod_index][offset] is actually t, the sequence is empty.
        """
        # This code is garbage. I'm tired.
        # It depends on every symbol being either a terminal or nonterminal,
        # so it is actually pretty broken probably.
        assert t in self.start_set_cache[prod_index][offset]

        def successors(pair):
            prod_index, offset = pair
            rhs = self.prods[prod_index].rhs
            nt = rhs[offset]
            if not self.grammar.is_nt(nt):
                return
            for next_prod_index, next_rhs in self.prods_with_indexes_by_nt[nt]:
                if t in self.start_set_cache[next_prod_index][0]:
                    yield next_prod_index, (next_prod_index, 0)

        def done(pair):
            prod_index, offset = pair
            rhs = self.prods[prod_index].rhs
            return rhs[offset] == t

        path = find_path([(prod_index, offset)],
                         successors,
                         done)
        if path is None:  # oh, we found a bug. this was likely.
            return

        for prod_index in path[1::2]:
            prod = self.prods[prod_index]
            yield prod.nt, prod.rhs

    def why_follow(self, nt, t):
        """ Return a sequence of productions showing why the terminal t is in nt's follow set. """

        start_points = {}
        for prod_index, prod in enumerate(self.prods):
            nt1 = prod.nt
            rhs1 = prod.rhs
            for i in range(len(rhs1) - 1):
                if self.grammar.is_nt(rhs1[i]) and t in self.start_set_cache[prod_index][i + 1]:
                    start_points[rhs1[i]] = (prod_index, i + 1)

        def successors(nt):
            for prod_index, rhs in self.prods_with_indexes_by_nt[nt]:
                last = rhs[-1]
                if self.grammar.is_nt(last):
                    yield prod_index, last

        path = find_path(start_points.keys(), successors, lambda point: point == nt)

        # Yield productions showing how to produce `nt` in the right context.
        prod_index, offset = start_points[path[0]]
        prod = self.prods[prod_index]
        yield prod.nt, prod.rhs
        for index in path[1::2]:
            prod = self.prods[index]
            yield prod.nt, prod.rhs

        # Now show how the immediate next token can expand into something that starts with `t`.
        for xnt, xrhs in self.why_start(t, prod_index, offset):
            yield xnt, xrhs

    def raise_shift_reduce_conflict(self, state, t, shift_options, nt, rhs):
        assert shift_options
        assert t in self.follow[nt]
        grammar = self.grammar
        some_shift_option = next(iter(shift_options))
        shift_option_nt = self.prods[some_shift_option.prod_index].nt
        shift_option_nt_str = grammar.element_to_str(shift_option_nt)
        t_str = grammar.element_to_str(t)
        scenario_str = state.traceback()

        raise ValueError("shift-reduce conflict when looking at {} followed by {}\n"
                         "can't decide whether to shift into:\n"
                         "    {}\n"
                         "or reduce using:\n"
                         "    {}\n"
                         "\n"
                         "These productions show how {} can appear after {} (if we reduce):\n"
                         "{}"
                         .format(scenario_str,
                                 t_str,
                                 grammar.lr_item_to_str(self.prods, some_shift_option),
                                 grammar.production_to_str(nt, rhs),
                                 t_str,
                                 nt,
                                 "".join("    " + grammar.production_to_str(nt, rhs) + "\n"
                                         for nt, rhs in self.why_follow(nt, t))))


class State:
    """A parser state. A state is basically a set of LRItems.

    During parser generation, states are annotated with attributes
    `.action_row` and `.ctn_row` that tell the actual parser what to do at run time.
    These will become rows of the parser tables.

    (For convenience, each State also has an attribute `self.context` that
    points to the PgenContext that has the grammar and various cached data; and
    an attribute `_debug_traceback` used in error messages. But for the most
    part, when we talk about a "state" we only care about the frozen set of
    LRItems in `self._lr_items`.)
    """

    __slots__ = [
        'context',
        '_lr_items', # OrderedSet of LRItems, the actual content here
        '_debug_traceback',  # State from which this one was first reached
        'key',  # str, projection from _lr_items used to merge similar-enough states
        '_hash',  # int, probably useless
        'action_row',  # output of analysis: {terminal: action}
        'ctn_row',  # output of analysis: {nonterminal: state_id}
        'id'  # int, small unique id
    ]

    def __init__(self, context, items, debug_traceback=None):
        self.context = context
        self._debug_traceback = debug_traceback

        # Consolidate similar items, to ensure that equivalent states have
        # equal _lr_items sets.
        a = collections.defaultdict(OrderedSet)
        for item in items:
            a[item.prod_index, item.offset, item.lookahead] |= item.followed_by
        self._lr_items = OrderedFrozenSet(LRItem(*k, OrderedFrozenSet(v)) for k, v in a.items())

        # This state should be reused if another state is found that has all
        # the same items except with different .followed_by sets. This line of
        # code is what makes this an LALR parser generator rather than a
        # canonical LR parser generator.
        self.key = "".join(repr((item.prod_index, item.offset, item.lookahead)) + "\n"
                           for item in sorted(self._lr_items))

        self._hash = hash(self.key)
        assert_items_are_compatible(context.grammar, context.prods, self._lr_items)

    def __eq__(self, other):
        return self.key == other.key

    def __hash__(self):
        return self._hash

    def __str__(self):
        return "{{{}}}".format(
            ",  ".join(self.context.grammar.lr_item_to_str(self.context.prods, item)
                       for item in self._lr_items)
        )

    def update(self, new_state):
        """Merge another State into self.

        This is called 0 or more times as we build out the graph of states.
        It's called each time an edge is found that points to `self`, except
        the first time. The caller has created a State object, `new_state`, but
        then found that this compatible State object already exists. Merge the
        two nodes. The caller discards `new_state` afterwards.

        Returns True if anything changed.
        """
        assert new_state.key == self.key
        assert len(self._lr_items) == len(new_state._lr_items)

        def item_key(item):
            return item.prod_index, item.offset, item.lookahead

        new_followed_by = {
            item_key(item): item.followed_by
            for item in new_state._lr_items
        }

        # If none of the new items adds any new followed_by symbols,
        # then there's nothing to update.
        if not any(new_followed_by[item_key(item)] - item.followed_by
                   for item in self._lr_items):
            return False

        # Really do the work of merging the two states.
        self._lr_items = OrderedFrozenSet(
            LRItem(*item_key(item), item.followed_by | new_followed_by[item_key(item)])
            for item in self._lr_items
        )
        return True

    def closure(self):
        """Compute transitive closure of this state under left-calls.

        That is, return a superset of self that adds every item that's
        reachable from it by "stepping in" to nonterminals without consuming
        any tokens. Note that it's often possible to "step in" repeatedly.

        This is the only part of the system that makes items with lookahead
        restrictions.
        """
        context = self.context
        grammar = context.grammar
        prods = context.prods
        prods_with_indexes_by_nt = context.prods_with_indexes_by_nt
        start_set_cache = context.start_set_cache

        closure = OrderedSet(self._lr_items)
        closure_todo = collections.deque(self._lr_items)
        while closure_todo:
            item = closure_todo.popleft()
            rhs = prods[item.prod_index].rhs
            if item.offset < len(rhs):
                next_symbol = rhs[item.offset]
                if grammar.is_nt(next_symbol):
                    # Step in to each production for this nt.
                    for dest_prod_index, callee_rhs in prods_with_indexes_by_nt[next_symbol]:
                        # We may have rewritten the grammar just a tad since
                        # `prods` was built. (`prods` has to be built during the
                        # expansion of optional elements, but the grammar has
                        # to be modified a bit after that.) So, embarrassingly, we
                        # must now check that the production we just found is
                        # still in the grammar. XXX FIXME
                        if callee_rhs or any(p.body == callee_rhs
                                             for p in grammar.nonterminals[next_symbol]):
                            ## print("    Considering stepping from item {} into production {}"
                            ##       .format(grammar.lr_item_to_str(prods, item),
                            ##               grammar.production_to_str(next_symbol, callee_rhs)))
                            followers = specific_follow(start_set_cache,
                                                        item.prod_index, item.offset,
                                                        item.followed_by)
                            new_item = context.make_lr_item(dest_prod_index, 0, item.lookahead,
                                                            followers)
                            if new_item is not None and new_item not in closure:
                                closure.add(new_item)
                                closure_todo.append(new_item)
        return closure

    def analyze(self, get_state_index, *, verbose=False):
        """Generate the LR parser table entry for this state.

        This is done without iterating or recursing on states. But we sometimes
        need state-ids for states we haven't considered yet, so it calls
        get_state_index() -- a callback that can enqueue new states to be
        visited later.
        """

        context = self.context
        grammar = context.grammar
        prods = context.prods
        follow = context.follow

        if verbose:
            print("State {}.".format(self.id))
            for item in self._lr_items:
                print("    " + grammar.lr_item_to_str(prods, item))
            print()

        # Step 1. Visit every item and list what we want to do for each
        # possible next token.
        shift_items = collections.defaultdict(OrderedSet)  # maps terminals to item-sets
        ctn_items = collections.defaultdict(OrderedSet)  # maps nonterminals to item-sets
        reduce_prods = {}  # maps follow-terminals to production indexes

        # Each item has three ways to advance.
        # - We can step over a terminal.
        # - We can step over a nonterminal.
        # - At the end of a production, we can reduce.
        # There is also a sort of "stepping in" effect for nonterminals, which
        # is achieved by the .closure() call at the top of the loop.
        for item in self.closure():
            offset = item.offset
            prod = prods[item.prod_index]
            nt = prod.nt
            i = prod.index
            rhs = prod.rhs
            if offset < len(rhs):
                next_symbol = rhs[offset]
                if grammar.is_terminal(next_symbol):
                    if lookahead_contains(item.lookahead, next_symbol):
                        next_item = context.make_lr_item(item.prod_index, offset + 1, None, item.followed_by)
                        if next_item is not None:
                            shift_items[next_symbol].add(next_item)
                else:
                    # The next element is always a terminal or nonterminal,
                    # never an Optional or Apply (those are preprocessed out of
                    # the grammar) or LookaheadRule (see make_lr_item).
                    assert grammar.is_nt(next_symbol)

                    # We never reduce with a lookahead restriction still
                    # active, so `lookahead=None` is appropriate.
                    next_item = context.make_lr_item(item.prod_index,
                                                     offset + 1,
                                                     lookahead=None,
                                                     followed_by=item.followed_by)
                    if next_item is not None:
                        ctn_items[next_symbol].add(next_item)
            else:
                if item.lookahead is not None:
                    # I think we could improve on this with canonical LR.
                    # The simplification in LALR might make it too weird though.
                    raise ValueError("invalid grammar: lookahead restriction still active "
                                     "at end of production " +
                                     grammar.production_to_str(nt, rhs))
                for t in item.followed_by:
                    if t in follow[nt]:
                        if t in reduce_prods:
                            context.raise_reduce_reduce_conflict(self, t, reduce_prods[t], item.prod_index)
                        reduce_prods[t] = item.prod_index

        # Step 2. Turn that information into table data to drive the parser.
        action_row = {}
        for t, shift_state in shift_items.items():
            shift_state = State(context, shift_state, self)  # freeze the set
            action_row[t] = get_state_index(shift_state)
        for t, prod_index in reduce_prods.items():
            prod = prods[prod_index]
            if t in action_row:
                context.raise_shift_reduce_conflict(self, t, shift_items[t], prod.nt, prod.rhs)
            # Encode reduce actions as negative numbers.
            # Negative zero is the same as zero, hence the "- 1".
            action_row[t] = ACCEPT if isinstance(prod.nt, InitNt) else -prod_index - 1
        ctn_row = {nt: get_state_index(State(context, ss, self))
                   for nt, ss in ctn_items.items()}
        self.action_row = action_row
        self.ctn_row = ctn_row

    def traceback(self):
        """Return a list of terminals and nonterminals that could have gotten us here."""
        # _debug_traceback chains all the way back to the initial state.
        traceback = []
        ss = self
        while ss is not None:
            traceback.append(ss)
            ss = ss._debug_traceback
        assert next(iter(traceback[-1]._lr_items)).offset == 0
        del traceback[-1]
        traceback.reverse()

        scenario = []
        for ss in traceback:
            item = next(iter(ss._lr_items))
            prod = self.context.prods[item.prod_index]
            assert item.offset > 0
            scenario.append(prod.rhs[item.offset - 1])
        return self.context.grammar.symbols_to_str(scenario)


def specific_follow(start_set_cache, prod_id, offset, followed_by):
    """Return the set of tokens that might appear after the nonterminal rhs[offset],
    given that after `rhs` the next token will be a terminal in `followed_by`.
    """

    # First, which tokens might follow rhs[offset] *within* the rest of rhs?
    result = start_set_cache[prod_id][offset+1]
    if EMPTY in result:
        # The rest of rhs might be empty, so we might also see `followed_by`.
        result = OrderedSet(result)
        result.remove(EMPTY)
        result |= followed_by
    return OrderedFrozenSet(result)


def analyze_states(context, prods, *, verbose=False, progress=False):
    """The core of the parser generation algorithm."""

    # There is one state for each reachable set of LR items.
    # Each reachable state's id is its index in `states`.
    states = []
    states_by_key = {}
    todo = collections.deque()

    def get_state_index(successor):
        """ Get a number for a state, assigning a new number if needed. """
        assert isinstance(successor, State)
        state = states_by_key.get(successor.key)
        if state is not None:
            if state.update(successor):
                todo.append(state)
        else:
            state = successor
            state.id = len(states)
            states.append(state)
            states_by_key[state.key] = state
            todo.append(state)
        return state.id

    # Compute the start states.
    init_state_map = {}
    for init_nt in context.grammar.init_nts:
        init_prod_index = prods.index(Prod(init_nt, 0, [init_nt.goal], removals=[], action="accept"))
        start_item = context.make_lr_item(init_prod_index,
                                          0,
                                          lookahead=None,
                                          followed_by=OrderedFrozenSet([END]))
        if start_item is None:
            init_state = State(context, [])
        else:
            init_state = State(context, [start_item])
        init_state_map[init_nt.goal] = get_state_index(init_state)

    # Turn the crank.
    i = 0
    while todo:
        if progress:
            sys.stdout.write(".")
            i += 1
            if i == 100:
                sys.stdout.write("\n")
                i = 0
            sys.stdout.flush()
        todo.popleft().analyze(get_state_index, verbose=verbose)

    if progress and i != 0:
        sys.stdout.write("\n")

    return states, init_state_map


def generate_parser(out, grammar, *, target='python',
                    verbose=False, progress=False):
    assert isinstance(grammar, Grammar)
    assert target in ('python', 'rust')

    # Step by step, we check the grammar and lower it to a more primitive form.
    grammar = expand_function_nonterminals(grammar)
    check_cycle_free(grammar)
    check_lookahead_rules(grammar)
    grammar = make_epsilon_free_step_1(grammar)
    grammar, prods, prods_with_indexes_by_nt = expand_all_optional_elements(grammar)
    grammar = make_epsilon_free_step_2(grammar)

    # Now the grammar is in its final form. Compute information about it that
    # we can cache and use during the main part of the algorithm below.
    start = start_sets(grammar)
    start_set_cache = make_start_set_cache(grammar, prods, start)
    follow = follow_sets(grammar, prods_with_indexes_by_nt, start_set_cache)
    context = PgenContext(grammar, prods, prods_with_indexes_by_nt, start_set_cache, follow)

    # Run the core LR table generation algorithm.
    states, init_state_map = analyze_states(context, prods, verbose=verbose,
                                            progress=progress)

    # Finally, dump the output.
    if target == 'rust':
        emit.write_rust_parser(out, grammar, states, prods, init_state_map)
    else:
        emit.write_python_parser(out, grammar, states, prods, init_state_map)


class Parser:
    pass


def compile_multi(grammar):
    assert isinstance(grammar, Grammar)
    out = io.StringIO()
    generate_parser(out, grammar)
    scope = {}
    ##print(out.getvalue())
    exec(out.getvalue(), scope)
    parser = Parser()
    for goal_nt in grammar.goals():
        name = "parse_" + goal_nt
        setattr(parser, name, scope[name])
    return parser


def compile(grammar):
    assert isinstance(grammar, Grammar)
    [goal] = grammar.goals()
    return getattr(compile_multi(grammar), "parse_" + goal)


# *** Fun demo ****************************************************************

def demo():
    grammar = example_grammar()

    import lexer
    tokenize = lexer.LexicalGrammar("+ - * / ( )", NUM=r'0|[1-9][0-9]*', VAR=r'[_A-Za-z]\w+')

    import io
    out = io.StringIO()
    generate_parser(out, grammar, ['expr'])
    code = out.getvalue()
    print(code)
    print("----")

    sandbox = {}
    exec(code, sandbox)
    parse = sandbox['parse_expr']

    while True:
        try:
            line = input('> ')
        except EOFError as _:
            break
        try:
            result = parse(tokenize(line))
        except Exception as exc:
            print(exc.__class__.__name__ + ": " + str(exc))
        else:
            print(result)


if __name__ == '__main__':
    demo()