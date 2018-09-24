from unittest import TestCase
from dbt.adapters.cache import RelationsCache
from dbt.adapters.default.relation import DefaultRelation


def make_mock_relationship(schema, identifier):
    return DefaultRelation.create(
        database='test_db', schema=schema, identifier=identifier,
        table_name=identifier, type='view'
    )


class TestCache(TestCase):
    def setUp(self):
        self.cache = RelationsCache()

    def test_empty(self):
        self.assertEqual(len(self.cache.relations), 0)
        relations = self.cache.get_relations('test')
        self.assertEqual(len(relations), 0)

        # make sure drop() is ok
        self.cache.drop('foo', 'bar')

    def test_retrieval(self):
        obj = object()
        self.cache.add('foo', 'bar', kind='view', inner=obj)
        self.assertEqual(len(self.cache.relations), 1)

        relations = self.cache.get_relations('foo')
        self.assertEqual(len(relations), 1)
        self.assertIs(relations[0], obj)

        relations = self.cache.get_relations('FOO')
        self.assertEqual(len(relations), 1)
        self.assertIs(relations[0], obj)

    def test_additions(self):
        obj = object()
        self.cache.add('foo', 'bar', kind='view')

        relations = self.cache.get_relations('foo')
        self.assertEqual(len(relations), 1)
        self.assertIs(relations[0], None)

        self.cache.add('foo', 'bar', inner=obj)
        self.assertEqual(len(self.cache.relations), 1)
        self.assertEqual(self.cache.schemas, {'foo'})

        relations = self.cache.get_relations('foo')
        self.assertEqual(len(relations), 1)
        self.assertIs(relations[0], obj)

        self.cache.add('FOO', 'baz', inner=object())
        self.assertEqual(len(self.cache.relations), 2)

        relations = self.cache.get_relations('foo')
        self.assertEqual(len(relations), 2)

        self.assertEqual(self.cache.schemas, {'foo', 'FOO'})
        self.cache._get_relation('foo', 'bar')
        self.cache._get_relation('FOO', 'baz')

    def test_rename(self):
        obj = make_mock_relationship('foo', 'bar')
        self.cache.add('foo', 'bar', kind='view', inner=obj)
        self.cache._get_relation('foo', 'bar')
        self.cache.rename_relation('foo', 'bar', 'foo', 'baz')

        relations = self.cache.get_relations('foo')
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0].schema, 'foo')
        self.assertEqual(relations[0].identifier, 'baz')

        relation = self.cache._get_relation('foo', 'baz')
        self.assertEqual(relation.inner.schema, 'foo')
        self.assertEqual(relation.inner.identifier, 'baz')
        self.assertEqual(relation.kind, 'view')
        self.assertEqual(relation.schema, 'foo')
        self.assertEqual(relation.identifier, 'baz')

        with self.assertRaises(KeyError):
            self.cache._get_relation('foo', 'bar')


class TestLikeDbt(TestCase):
    def setUp(self):
        self.cache = RelationsCache()

        self.stored_relations = {}
        # add a bunch of cache entries
        for ident in 'abcdef':
            obj = self.stored_relations.setdefault(
                ident,
                make_mock_relationship('schema', ident)
            )
            self.cache.add('schema', ident, kind='view', inner=obj)
        # 'b' references 'a'
        self.cache.add_link('schema', 'a', 'schema', 'b')
        # and 'c' references 'b'
        self.cache.add_link('schema', 'b', 'schema', 'c')
        # and 'd' references 'b'
        self.cache.add_link('schema', 'b', 'schema', 'd')
        # and 'e' references 'a'
        self.cache.add_link('schema', 'a', 'schema', 'e')
        # and 'f' references 'd'
        self.cache.add_link('schema', 'd', 'schema', 'f')
        # so drop propagation goes (a -> (b -> (c (d -> f))) e)

    def assert_has_relations(self, expected):
        current = set(r.identifier for r in self.cache.get_relations('schema'))
        self.assertEqual(current, expected)

    def test_drop_inner(self):
        self.assert_has_relations(set('abcdef'))
        self.cache.drop('schema', 'b')
        self.assert_has_relations({'a', 'e'})

    def test_rename_and_drop(self):
        self.assert_has_relations(set('abcdef'))
        # drop the backup/tmp
        self.cache.drop('schema', 'b__backup')
        self.cache.drop('schema', 'b__tmp')
        self.assert_has_relations(set('abcdef'))
        # create a new b__tmp
        self.cache.add('schema', 'b__tmp', kind='view',
            inner=make_mock_relationship('schema', 'b__tmp')
        )
        self.assert_has_relations(set('abcdef') | {'b__tmp'})
        # rename b -> b__backup
        self.cache.rename_relation('schema', 'b', 'schema', 'b__backup')
        self.assert_has_relations(set('acdef') | {'b__tmp', 'b__backup'})
        # rename temp to b
        self.cache.rename_relation('schema', 'b__tmp', 'schema', 'b')
        self.assert_has_relations(set('abcdef') | {'b__backup'})

        # drop backup, everything that used to depend on b should be gone, but
        # b itself should still exist
        self.cache.drop('schema', 'b__backup')
        self.assert_has_relations(set('abe'))
        relation = self.cache._get_relation('schema', 'a')
        self.assertEqual(len(relation.referenced_by), 1)


class TestComplexCache(TestCase):
    def setUp(self):
        self.cache = RelationsCache()
        inputs = [
            ('foo', 'table1', 'table'),
            ('bar', 'table2', 'view'),
            ('foo', 'table3', 'view'),
            ('foo', 'table4', 'view'),
            ('bar', 'table3', 'view'),
        ]
        self.inputs = [
            (s, i, k, make_mock_relationship(s, i))
            for s, i, k in inputs
        ]
        for schema, ident, kind, inner in self.inputs:
            self.cache.add(schema, ident, kind, inner)

        # foo.table3 references foo.table1
        # (create view table3 as (select * from table1...))
        self.cache.add_link(
            'foo', 'table1',
            'foo', 'table3'
        )
        # bar.table3 references foo.table3
        # (create view bar.table5 as (select * from foo.table3...))
        self.cache.add_link(
            'foo', 'table3',
            'bar', 'table3'
        )

        # foo.table2 also references foo.table1
        self.cache.add_link(
            'foo', 'table1',
            'foo', 'table4',
        )

    def test_get_relations(self):
        self.assertEqual(len(self.cache.get_relations('foo')), 3)
        self.assertEqual(len(self.cache.get_relations('bar')), 2)
        self.assertEqual(len(self.cache.relations), 5)

    def test_drop_one(self):
        # dropping bar.table2 should only drop itself
        self.cache.drop('bar', 'table2')
        self.assertEqual(len(self.cache.get_relations('foo')), 3)
        self.assertEqual(len(self.cache.get_relations('bar')), 1)
        self.assertEqual(len(self.cache.relations), 4)

    def test_drop_many(self):
        # dropping foo.table1 should drop everything but bar.table2.
        self.cache.drop('foo', 'table1')
        self.assertEqual(len(self.cache.get_relations('foo')), 0)
        self.assertEqual(len(self.cache.get_relations('bar')), 1)
        self.assertEqual(len(self.cache.relations), 1)

    def test_rename_root(self):
        self.cache.rename_relation('foo', 'table1', 'bar', 'table1')
        retrieved = self.cache._get_relation('bar','table1').inner
        self.assertEqual(retrieved.schema, 'bar')
        self.assertEqual(retrieved.identifier, 'table1')
        self.assertEqual(len(self.cache.get_relations('foo')), 2)
        self.assertEqual(len(self.cache.get_relations('bar')), 3)

        # make sure drops still cascade from the renamed table
        self.cache.drop('bar', 'table1')
        self.assertEqual(len(self.cache.get_relations('foo')), 0)
        self.assertEqual(len(self.cache.get_relations('bar')), 1)
        self.assertEqual(len(self.cache.relations), 1)

    def test_rename_branch(self):
        self.cache.rename_relation('foo', 'table3', 'foo', 'table2')
        self.assertEqual(len(self.cache.get_relations('foo')), 3)
        self.assertEqual(len(self.cache.get_relations('bar')), 2)

        # make sure drops still cascade through the renamed table
        self.cache.drop('foo', 'table1')
        self.assertEqual(len(self.cache.get_relations('foo')), 0)
        self.assertEqual(len(self.cache.get_relations('bar')), 1)
        self.assertEqual(len(self.cache.relations), 1)
