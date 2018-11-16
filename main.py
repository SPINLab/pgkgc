#! /usr/bin/env python

from collections import Counter
import logging

from rdflib.namespace import RDF, XSD
from rdflib.graph import Literal, URIRef

from structures import Clause, GenerationForest, GenerationTree
from cache import Cache
from utils import support_of, confidence_of


IGNORE_PREDICATES = {RDF.type}
IDENTITY = URIRef("local://identity")  # reflexive property

log = logging.getLogger(__name__)

def generate(g, max_depth, min_support, min_confidence):
    cache = Cache(g, min_support)
    generation_forest = init_generation_forest(g, cache.object_type_map,
                                               min_support, min_confidence)

    for depth in range(0, max_depth):
        log.debug("Generating depth {} / {}".format(depth+1, max_depth))
        for ctype in generation_forest.types():
            derivatives = set()

            for clause in generation_forest.get(ctype, depth):
                # only consider unbound object type variables as an extension of
                # a bound entity is already implicitly included
                pendant_incidents = {assertion for assertion in clause.body.distances[depth]
                                        if type(assertion.rhs) is Clause.ObjectTypeVariable}
                derivatives |= explore(g,
                                       generation_forest,
                                       clause,
                                       pendant_incidents,
                                       cache,
                                       min_support,
                                       min_confidence)

            generation_forest.add(ctype, derivatives, depth+1)

    return generation_forest

def explore(g, generation_forest, clause, pendant_incidents, cache, min_support, min_confidence):
    extended_clauses = set()

    while len(pendant_incidents) > 0:
        pendant_incident = pendant_incidents.pop()

        # gather all possible extensions for an entity of type t
        candidate_extensions = {clause.head for clause in
                                generation_forest.get(pendant_incident.rhs.type, 0)}

        # evaluate all candidate extensions for this depth
        extensions = extend(g, clause, pendant_incident, candidate_extensions,
                            cache, min_support, min_confidence)
        extended_clauses |= extensions

        for extended_clause in extended_clauses:
            extended_clauses |= explore(g, generation_forest, extended_clause,
                                        pendant_incidents, cache, min_support,
                                        min_confidence)

    return extended_clauses

def extend(g, parent, pendant_incident, candidate_extensions, cache,
           min_support, min_confidence):
    extended_clauses = set()

    while len(candidate_extensions) > 0:
        candidate_extension = candidate_extensions.pop()

        # create new clause body by extending that of the parent
        head = parent.head
        body = parent.body.copy()
        body.extend(endpoint=pendant_incident, extension=candidate_extension.copy())

        # compute support
        support, satisfies_body = support_of(cache.predicate_map,
                                             cache.object_type_map,
                                             cache.data_type_map,
                                             body,
                                             body.identity,
                                             parent._satisfy_body,
                                             min_support)
        if support >= min_support and not support >= parent.support:
            # compute confidence
            confidence, satisfies_full = confidence_of(cache.predicate_map,
                                                       cache.object_type_map,
                                                       cache.data_type_map,
                                                       head,
                                                       satisfies_body)
            if confidence < min_confidence:
                continue

            # save more constraint clause
            extented_clause = Clause(head=head,
                                     body=body,
                                     parent=parent)
            extented_clause._satisfy_body = satisfies_body
            extented_clause._satisfy_full = satisfies_full

            extented_clause.support = support
            extented_clause.confidence = confidence
            extented_clause.domain_probability = confidence / support

            # recalculate predicate frequency from parent
            pfreq = parent.confidence / parent.range_probability
            extented_clause.range_probability = confidence / pfreq

            extended_clauses.add(extented_clause)

            for extented_clause in extended_clauses:
                extended_clauses |= extend(g,
                                           extented_clause,
                                           pendant_incident,
                                           {ext for ext in candidate_extensions},
                                           cache,
                                           min_support,
                                           min_confidence)

    return extended_clauses

def init_generation_forest(g, class_instance_map, min_support, min_confidence):
    log.debug("Initializing Generation Forest")
    generation_forest = GenerationForest()

    for t in class_instance_map['type-to-object'].keys():
        # if the number of type instances do not exceed the minimal support then
        # any pattern of this type will not either
        support = len(class_instance_map['type-to-object'][t])
        if support < min_support:
            continue

        log.debug("Initializing Generation Tree for type {}".format(str(t)))
        # gather all predicate-object pairs belonging to the members of a type
        predicate_object_map = dict()
        for e in class_instance_map['type-to-object'][t]:
            for _, p, o in g.triples((e, None, None)):
                if p in IGNORE_PREDICATES:
                    continue

                if p not in predicate_object_map.keys():
                    predicate_object_map[p] = dict()
                if o not in predicate_object_map[p].keys():
                    predicate_object_map[p][o] = 0

                predicate_object_map[p][o] = predicate_object_map[p][o] + 1

        # create shared variables
        parent = Clause(head=True, body={})
        var = Clause.ObjectTypeVariable(type=t)

        # generate clauses for each predicate-object pair
        generation_tree = GenerationTree()
        for p in predicate_object_map.keys():
            pfreq = sum(predicate_object_map[p].values())
            if pfreq < min_support:
                continue

            # create clauses for all predicate-object pairs
            object_types = list()
            object_types_map = dict()
            data_types = list()
            data_types_map = dict()
            for o in predicate_object_map[p].keys():
                # TODO: allow prior background knowledge
                # map resources to types for unbound type generation
                if type(o) is URIRef:
                    ctype = g.value(e, RDF.type)
                    object_types.append(ctype)

                    if ctype not in object_types_map.keys():
                        object_types_map[ctype] = set()
                    object_types_map[ctype].add(o)
                if type(o) is Literal:
                    dtype = o.datatype
                    if dtype is None:
                        dtype = XSD.string if o.language != None else XSD.anyType

                    data_types.append(dtype)
                    if dtype not in data_types_map.keys():
                        data_types_map[dtype] = set()
                    data_types_map[dtype].add(o)

                # TODO: skip if distribution is (close to) uniform
                # create new clause
                phi = Clause(head=Clause.Assertion(var, p, o),
                             body=Clause.Body(identity=Clause.IdentityAssertion(var, IDENTITY, var)),
                             parent=parent)

                phi._satisfy_body = class_instance_map['type-to-object'][t]
                phi._satisfy_full = {e for e in class_instance_map['type-to-object'][t] if (e, p, o) in g}

                phi.support = len(phi._satisfy_body)
                phi.confidence = len(phi._satisfy_full)
                phi.domain_probability = phi.confidence/phi.support
                phi.range_probability = phi.confidence/pfreq

                if phi.confidence >= min_confidence:
                    generation_tree.add(phi, depth=0)

            # generate unbound object type assertions
            objecttype_count = Counter(object_types)
            for ctype, ofreq in objecttype_count.items():
                if ctype is None:
                    continue

                var_o = Clause.ObjectTypeVariable(type=ctype)
                phi = Clause(head=Clause.Assertion(var, p, var_o),
                             body=Clause.Body(identity=Clause.IdentityAssertion(var, IDENTITY, var)),
                             parent=parent)

                phi._satisfy_body = class_instance_map['type-to-object'][t]
                phi._satisfy_full = object_types_map[ctype]

                phi.support = len(phi._satisfy_body)
                phi.confidence = len(phi._satisfy_full)
                phi.domain_probability = phi.confidence/phi.support
                phi.range_probability = phi.confidence/pfreq

                if phi.confidence >= min_confidence:
                    generation_tree.add(phi, depth=0)

            # generate unbound data type assertions
            datatype_count = Counter(data_types)
            for dtype, ofreq in datatype_count.items():
                if dtype is None:
                    continue

                var_o = Clause.DataTypeVariable(type=dtype)
                phi = Clause(head=Clause.Assertion(var, p, var_o),
                             body=Clause.Body(identity=Clause.IdentityAssertion(var, IDENTITY, var)),
                             parent=parent)

                phi._satisfy_body = class_instance_map['type-to-object'][t]
                phi._satisfy_full = data_types_map[dtype]

                phi.support = len(phi._satisfy_body)
                phi.confidence = len(phi._satisfy_full)
                phi.domain_probability = phi.confidence/phi.support
                phi.range_probability = phi.confidence/pfreq

                if phi.confidence >= min_confidence:
                    generation_tree.add(phi, depth=0)

        generation_forest.plant(t, generation_tree)

    return generation_forest
