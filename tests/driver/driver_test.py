import unittest.mock as mock
import threading
import collections
from unittest import TestCase

from esrally import metrics, track, exceptions
from esrally.driver import driver
from esrally.track import params
from esrally.utils import io


class DriverTestParamSource:
    def __init__(self, indices=None, params=None):
        if params is None:
            params = {}
        self._indices = indices
        self._params = params

    def partition(self, partition_index, total_partitions):
        return self

    def size(self):
        return self._params["size"] if "size" in self._params else 1

    def params(self):
        return self._params


class ScheduleTestCase(TestCase):
    def assert_schedule(self, expected_schedule, schedule):
        idx = 0
        for invocation_time, sample_type, progress_percent, runner, params in schedule:
            exp_invocation_time, exp_sample_type, exp_progress_percent, exp_params = expected_schedule[idx]
            self.assertAlmostEqual(exp_invocation_time, invocation_time, msg="Expected invocation time does not match")
            self.assertEqual(exp_sample_type, sample_type, "Sample type does not match")
            self.assertEqual(exp_progress_percent, progress_percent, "Current progress does not match")
            self.assertIsNotNone(runner, "runner must be defined")
            self.assertEqual(exp_params, params, "Parameters do not match")
            idx += 1


class AllocatorTests(TestCase):
    def setUp(self):
        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)

    def test_allocates_one_task(self):
        op = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")
        task = track.Task(op)

        allocator = driver.Allocator([task])

        self.assertEqual(1, allocator.clients)
        self.assertEqual(3, len(allocator.allocations[0]))
        self.assertEqual(2, len(allocator.join_points))
        self.assertEqual([{op}], allocator.operations_per_joinpoint)

    def test_allocates_two_serial_tasks(self):
        op = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")
        task = track.Task(op)

        allocator = driver.Allocator([task, task])

        self.assertEqual(1, allocator.clients)
        # we have two operations and three join points
        self.assertEqual(5, len(allocator.allocations[0]))
        self.assertEqual(3, len(allocator.join_points))
        self.assertEqual([{op}, {op}], allocator.operations_per_joinpoint)

    def test_allocates_two_parallel_tasks(self):
        op = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")
        task = track.Task(op)

        allocator = driver.Allocator([track.Parallel([task, task])])

        self.assertEqual(2, allocator.clients)
        self.assertEqual(3, len(allocator.allocations[0]))
        self.assertEqual(3, len(allocator.allocations[1]))
        self.assertEqual(2, len(allocator.join_points))
        self.assertEqual([{op}], allocator.operations_per_joinpoint)

    def test_allocates_mixed_tasks(self):
        op1 = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")
        op2 = track.Operation("stats", track.OperationType.IndicesStats, param_source="driver-test-param-source")
        op3 = track.Operation("search", track.OperationType.Search, param_source="driver-test-param-source")

        index = track.Task(op1)
        stats = track.Task(op2)
        search = track.Task(op3)

        allocator = driver.Allocator([index,
                                      track.Parallel([index, stats, stats]),
                                      index,
                                      index,
                                      track.Parallel([search, search, search])])

        self.assertEqual(3, allocator.clients)

        # 1 join point, 1 op, 1 jp, 1 (parallel) op, 1 jp, 1 op, 1 jp, 1 op, 1 jp, 1 (parallel) op, 1 jp
        self.assertEqual(11, len(allocator.allocations[0]))
        self.assertEqual(11, len(allocator.allocations[1]))
        self.assertEqual(11, len(allocator.allocations[2]))
        self.assertEqual(6, len(allocator.join_points))
        self.assertEqual([{op1}, {op1, op2}, {op1}, {op1}, {op3}], allocator.operations_per_joinpoint)

    def test_allocates_more_tasks_than_clients(self):
        op1 = track.Operation("index-a", track.OperationType.Index, param_source="driver-test-param-source")
        op2 = track.Operation("index-b", track.OperationType.Index, param_source="driver-test-param-source")
        op3 = track.Operation("index-c", track.OperationType.Index, param_source="driver-test-param-source")
        op4 = track.Operation("index-d", track.OperationType.Index, param_source="driver-test-param-source")
        op5 = track.Operation("index-e", track.OperationType.Index, param_source="driver-test-param-source")

        index_a = track.Task(op1)
        index_b = track.Task(op2)
        index_c = track.Task(op3)
        index_d = track.Task(op4)
        index_e = track.Task(op5)

        allocator = driver.Allocator([track.Parallel(tasks=[index_a, index_b, index_c, index_d, index_e], clients=2)])

        self.assertEqual(2, allocator.clients)

        allocations = allocator.allocations

        self.assertEqual(2, len(allocations))
        # join_point, index_a, index_c, index_e, join_point
        self.assertEqual(5, len(allocations[0]))
        # we really have no chance to extract the join point so we just take what is there...
        self.assertEqual([allocations[0][0], index_a, index_c, index_e, allocations[0][4]], allocations[0])
        # join_point, index_a, index_c, None, join_point
        self.assertEqual(5, len(allocator.allocations[1]))
        self.assertEqual([allocations[1][0], index_b, index_d, None, allocations[1][4]], allocations[1])

        self.assertEqual([{op1, op2, op3, op4, op5}], allocator.operations_per_joinpoint)

    def test_considers_number_of_clients_per_subtask(self):
        op1 = track.Operation("index-a", track.OperationType.Index, param_source="driver-test-param-source")
        op2 = track.Operation("index-b", track.OperationType.Index, param_source="driver-test-param-source")
        op3 = track.Operation("index-c", track.OperationType.Index, param_source="driver-test-param-source")

        index_a = track.Task(op1)
        index_b = track.Task(op2)
        index_c = track.Task(op3, clients=2)

        allocator = driver.Allocator([track.Parallel(tasks=[index_a, index_b, index_c], clients=3)])

        self.assertEqual(3, allocator.clients)

        allocations = allocator.allocations

        self.assertEqual(3, len(allocations))
        # join_point, index_a, index_c, join_point
        self.assertEqual(4, len(allocations[0]))
        # we really have no chance to extract the join point so we just take what is there...
        self.assertEqual([allocations[0][0], index_a, index_c, allocations[0][3]], allocations[0])
        # join_point, index_b, None, join_point
        self.assertEqual(4, len(allocator.allocations[1]))
        self.assertEqual([allocations[1][0], index_b, None, allocations[1][3]], allocations[1])

        self.assertEqual(4, len(allocator.allocations[2]))
        self.assertEqual([allocations[2][0], index_c, None, allocations[2][3]], allocations[2])

        self.assertEqual([{op1, op2, op3}], allocator.operations_per_joinpoint)


class IndexManagementTests(TestCase):
    @mock.patch("elasticsearch.Elasticsearch")
    def test_setup_auto_managed_index(self, es):
        es.indices.exists.return_value = False

        index = track.Index(name="test-index",
                            auto_managed=True,
                            types=[track.Type(name="test-type", mapping_file=['{"test-type": "empty-for-test"}'])])
        index_settings = {
            "index.number_of_replicas": 0
        }
        expected_body = {
            "settings": {
                "index.number_of_replicas": 0
            },
            "mappings": {
                "test-type": "empty-for-test"
            }
        }
        driver.setup_index(es, index, index_settings, source=io.StringAsFileSource)

        es.indices.exists.assert_called_with(index="test-index")
        es.indices.delete.assert_not_called()
        es.indices.create.assert_called_with(index="test-index", body=expected_body)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_recreate_existing_managed_index(self, es):
        es.indices.exists.return_value = True

        index = track.Index(name="test-index",
                            auto_managed=True,
                            types=[track.Type(name="test-type", mapping_file=['{"test-type": "empty-for-test"}'])])
        index_settings = {
            "index.number_of_replicas": 0
        }
        expected_body = {
            "settings": {
                "index.number_of_replicas": 0
            },
            "mappings": {
                "test-type": "empty-for-test"
            }
        }
        driver.setup_index(es, index, index_settings, source=io.StringAsFileSource)

        es.indices.exists.assert_called_with(index="test-index")
        es.indices.delete.assert_called_with(index="test-index")
        es.indices.create.assert_called_with(index="test-index", body=expected_body)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_do_not_change_manually_managed_index(self, es):
        index = track.Index(name="test-index", auto_managed=False, types=[])
        driver.setup_index(es, index, index_settings=None)
        es.assert_not_called()


class MetricsAggregationTests(TestCase):
    def setUp(self):
        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)

    def test_different_sample_types(self):
        op = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")

        samples = [
            driver.Sample(0, 1470838595, 21, op, metrics.SampleType.Warmup, None, -1, -1, 3000, "docs", 1, 1),
            driver.Sample(0, 1470838595.5, 21.5, op, metrics.SampleType.Normal, None, -1, -1, 2500, "docs", 1, 1),
        ]

        aggregated = driver.calculate_global_throughput(samples)

        self.assertIn(op, aggregated)
        self.assertEqual(1, len(aggregated))

        throughput = aggregated[op]
        self.assertEqual(2, len(throughput))
        self.assertEqual((1470838595, 21, metrics.SampleType.Warmup, 3000, "docs/s"), throughput[0])
        self.assertEqual((1470838595.5, 21.5, metrics.SampleType.Normal, 3666.6666666666665, "docs/s"), throughput[1])

    def test_single_metrics_aggregation(self):
        op = track.Operation("index", track.OperationType.Index, param_source="driver-test-param-source")

        samples = [
            driver.Sample(0, 1470838595, 21, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 1, 1 / 9),
            driver.Sample(0, 1470838596, 22, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 2, 2 / 9),
            driver.Sample(0, 1470838597, 23, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 3, 3 / 9),
            driver.Sample(0, 1470838598, 24, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 4, 4 / 9),
            driver.Sample(0, 1470838599, 25, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 5, 5 / 9),
            driver.Sample(0, 1470838600, 26, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 6, 6 / 9),
            driver.Sample(1, 1470838598.5, 24.5, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 4.5, 7 / 9),
            driver.Sample(1, 1470838599.5, 25.5, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 5.5, 8 / 9),
            driver.Sample(1, 1470838600.5, 26.5, op, metrics.SampleType.Normal, None, -1, -1, 5000, "docs", 6.5, 9 / 9)
        ]

        aggregated = driver.calculate_global_throughput(samples)

        self.assertIn(op, aggregated)
        self.assertEqual(1, len(aggregated))

        throughput = aggregated[op]
        self.assertEqual(6, len(throughput))
        self.assertEqual((1470838595, 21, metrics.SampleType.Normal, 5000, "docs/s"), throughput[0])
        self.assertEqual((1470838596, 22, metrics.SampleType.Normal, 5000, "docs/s"), throughput[1])
        self.assertEqual((1470838597, 23, metrics.SampleType.Normal, 5000, "docs/s"), throughput[2])
        self.assertEqual((1470838598, 24, metrics.SampleType.Normal, 5000, "docs/s"), throughput[3])
        self.assertEqual((1470838599, 25, metrics.SampleType.Normal, 6000, "docs/s"), throughput[4])
        self.assertEqual((1470838600, 26, metrics.SampleType.Normal, 6666.666666666667, "docs/s"), throughput[5])
        # self.assertEqual((1470838600.5, 26.5, metrics.SampleType.Normal, 10000), throughput[6])


class SchedulerTests(ScheduleTestCase):
    def setUp(self):
        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)
        self.test_track = track.Track(name="unittest", short_description="unittest track",
                                      source_root_url="http://example.org",
                                      indices=None,
                                      challenges=None)

    def test_search_task_one_client(self):
        task = track.Task(track.Operation("search", track.OperationType.Search.name, param_source="driver-test-param-source"),
                          warmup_iterations=3, iterations=5, clients=1, params={"target-throughput": 10, "clients": 1})
        schedule = driver.schedule_for(self.test_track, task, 0)

        expected_schedule = [
            (0, metrics.SampleType.Warmup, 1 / 8, {}),
            (0.1, metrics.SampleType.Warmup, 2 / 8, {}),
            (0.2, metrics.SampleType.Warmup, 3 / 8, {}),
            (0.3, metrics.SampleType.Normal, 4 / 8, {}),
            (0.4, metrics.SampleType.Normal, 5 / 8, {}),
            (0.5, metrics.SampleType.Normal, 6 / 8, {}),
            (0.6, metrics.SampleType.Normal, 7 / 8, {}),
            (0.7, metrics.SampleType.Normal, 8 / 8, {}),
        ]
        self.assert_schedule(expected_schedule, schedule)

    def test_search_task_two_clients(self):
        task = track.Task(track.Operation("search", track.OperationType.Search.name, param_source="driver-test-param-source"),
                          warmup_iterations=2, iterations=10, clients=2, params={"target-throughput": 10, "clients": 2})
        schedule = driver.schedule_for(self.test_track, task, 0)

        expected_schedule = [
            (0, metrics.SampleType.Warmup, 1 / 6, {}),
            (0.2, metrics.SampleType.Normal, 2 / 6, {}),
            (0.4, metrics.SampleType.Normal, 3 / 6, {}),
            (0.6, metrics.SampleType.Normal, 4 / 6, {}),
            (0.8, metrics.SampleType.Normal, 5 / 6, {}),
            (1.0, metrics.SampleType.Normal, 6 / 6, {}),
        ]
        self.assert_schedule(expected_schedule, schedule)

    def test_schedule_for_warmup_time_based(self):
        task = track.Task(track.Operation("time-based", track.OperationType.Index.name, params={"body": ["a"], "size": 11},
                                          param_source="driver-test-param-source"),
                          warmup_time_period=0, clients=4, params={"target-throughput": 4, "clients": 4})

        invocations = driver.schedule_for(self.test_track, task, 0)

        self.assert_schedule([
            (0.0, metrics.SampleType.Normal, 1 / 11, {"body": ["a"], "size": 11}),
            (1.0, metrics.SampleType.Normal, 2 / 11, {"body": ["a"], "size": 11}),
            (2.0, metrics.SampleType.Normal, 3 / 11, {"body": ["a"], "size": 11}),
            (3.0, metrics.SampleType.Normal, 4 / 11, {"body": ["a"], "size": 11}),
            (4.0, metrics.SampleType.Normal, 5 / 11, {"body": ["a"], "size": 11}),
            (5.0, metrics.SampleType.Normal, 6 / 11, {"body": ["a"], "size": 11}),
            (6.0, metrics.SampleType.Normal, 7 / 11, {"body": ["a"], "size": 11}),
            (7.0, metrics.SampleType.Normal, 8 / 11, {"body": ["a"], "size": 11}),
            (8.0, metrics.SampleType.Normal, 9 / 11, {"body": ["a"], "size": 11}),
            (9.0, metrics.SampleType.Normal, 10 / 11, {"body": ["a"], "size": 11}),
            (10.0, metrics.SampleType.Normal, 11 / 11, {"body": ["a"], "size": 11}),
        ], list(invocations))

    def test_schedule_for_time_based(self):
        task = track.Task(track.Operation("time-based", track.OperationType.Index.name, params={"body": ["a"], "size": 11},
                                          param_source="driver-test-param-source"), warmup_time_period=0.1, time_period=0.1, clients=1)

        invocations = list(driver.schedule_for(self.test_track, task, 0))

        self.assertTrue(len(invocations) > 0)

        last_progress = -1

        for invocation_time, sample_type, progress_percent, runner, params in invocations:
            # we're not throughput throttled
            self.assertEqual(0, invocation_time)
            if progress_percent <= 0.5:
                self.assertEqual(metrics.SampleType.Warmup, sample_type)
            else:
                self.assertEqual(metrics.SampleType.Normal, sample_type)
            self.assertTrue(last_progress < progress_percent)
            last_progress = progress_percent
            self.assertTrue(round(progress_percent, 2) >= 0.0, "progress should be >= 0.0 but was [%f]" % progress_percent)
            self.assertTrue(round(progress_percent, 2) <= 1.0, "progress should be <= 1.0 but was [%f]" % progress_percent)
            self.assertIsNotNone(runner, "runner must be defined")
            self.assertEqual({"body": ["a"], "size": 11}, params)


class ExecutorTests(TestCase):
    class NoopContextManager:
        def __init__(self, mock):
            self.mock = mock

        def __enter__(self):
            return self

        def __call__(self, *args):
            return self.mock(*args)

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def __str__(self):
            return str(self.mock)

    def context_managed(self, mock):
        return ExecutorTests.NoopContextManager(mock)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_execute_schedule_in_throughput_mode(self, es):
        es.bulk.return_value = {
            "errors": False
        }

        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)
        test_track = track.Track(name="unittest", short_description="unittest track",
                                 source_root_url="http://example.org",
                                 indices=None,
                                 challenges=None)

        task = track.Task(track.Operation("time-based", track.OperationType.Index.name, params={
            "body": ["action_metadata_line", "index_line"],
            "action_metadata_present": True,
            "bulk-size": 1
        },
                                          param_source="driver-test-param-source"),
                          warmup_time_period=0, clients=4)
        schedule = driver.schedule_for(test_track, task, 0)

        sampler = driver.Sampler(client_id=2, task=task, start_timestamp=100)
        cancel = threading.Event()
        driver.execute_schedule(cancel, 0, task.operation, schedule, es, sampler)

        samples = sampler.samples

        self.assertTrue(len(samples) > 0)
        previous_absolute_time = -1.0
        previous_relative_time = -1.0
        for sample in samples:
            self.assertEqual(2, sample.client_id)
            self.assertEqual(task, sample.task)
            self.assertTrue(previous_absolute_time < sample.absolute_time)
            previous_absolute_time = sample.absolute_time
            self.assertTrue(previous_relative_time < sample.relative_time)
            previous_relative_time = sample.relative_time
            # we don't have any warmup time period
            self.assertEqual(metrics.SampleType.Normal, sample.sample_type)
            # latency equals service time in throughput mode
            self.assertEqual(sample.latency_ms, sample.service_time_ms)
            self.assertEqual(1, sample.total_ops)
            self.assertEqual("docs", sample.total_ops_unit)
            self.assertEqual(1, sample.request_meta_data["bulk-size"])

    @mock.patch("elasticsearch.Elasticsearch")
    def test_execute_schedule_throughput_throttled(self, es):
        es.bulk.return_value = {
            "errors": False
        }

        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)
        test_track = track.Track(name="unittest", short_description="unittest track",
                                 source_root_url="http://example.org",
                                 indices=None,
                                 challenges=None)

        # in one second (0.5 warmup + 0.5 measurement) we should get 1000 [ops/s] / 4 [clients] = 250 samples
        for target_throughput, bounds in {10: [2, 4], 100: [24, 26], 1000: [245, 255]}.items():
            task = track.Task(track.Operation("time-based", track.OperationType.Index.name, params={
                "body": ["action_metadata_line", "index_line"],
                "action_metadata_present": True,
                "bulk-size": 1
            },
                                              param_source="driver-test-param-source"),
                              warmup_time_period=0.5, time_period=0.5, clients=4,
                              params={"target-throughput": target_throughput, "clients": 4})
            schedule = driver.schedule_for(test_track, task, 0)
            sampler = driver.Sampler(client_id=0, task=task, start_timestamp=0)

            cancel = threading.Event()
            driver.execute_schedule(cancel, 0, task.operation, schedule, es, sampler)

            samples = sampler.samples

            sample_size = len(samples)
            lower_bound = bounds[0]
            upper_bound = bounds[1]
            self.assertTrue(lower_bound <= sample_size <= upper_bound,
                            msg="Expected sample size to be between %d and %d but was %d" % (lower_bound, upper_bound, sample_size))

    @mock.patch("elasticsearch.Elasticsearch")
    def test_cancel_execute_schedule(self, es):
        es.bulk.return_value = {
            "errors": False
        }

        params.register_param_source_for_name("driver-test-param-source", DriverTestParamSource)
        test_track = track.Track(name="unittest", short_description="unittest track",
                                 source_root_url="http://example.org",
                                 indices=None,
                                 challenges=None)

        # in one second (0.5 warmup + 0.5 measurement) we should get 1000 [ops/s] / 4 [clients] = 250 samples
        for target_throughput, bounds in {10: [2, 4], 100: [24, 26], 1000: [245, 255]}.items():
            task = track.Task(track.Operation("time-based", track.OperationType.Index.name, params={
                "body": ["action_metadata_line", "index_line"],
                "action_metadata_present": True,
                "bulk-size": 1
            },
                                              param_source="driver-test-param-source"),
                              warmup_time_period=0.5, time_period=0.5, clients=4,
                              params={"target-throughput": target_throughput, "clients": 4})
            schedule = driver.schedule_for(test_track, task, 0)
            sampler = driver.Sampler(client_id=0, task=task, start_timestamp=0)

            cancel = threading.Event()
            cancel.set()
            driver.execute_schedule(cancel, 0, task.operation, schedule, es, sampler)

            samples = sampler.samples

            sample_size = len(samples)
            self.assertEqual(0, sample_size)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_execute_schedule_aborts_on_error(self, es):
        class ExpectedUnitTestException(Exception):
            pass

        def run(*args, **kwargs):
            raise ExpectedUnitTestException()

        schedule = [(0, metrics.SampleType.Warmup, 0, self.context_managed(run), None)]
        sampler = driver.Sampler(client_id=0, task=None, start_timestamp=0)
        cancel = threading.Event()
        with self.assertRaises(ExpectedUnitTestException):
            driver.execute_schedule(cancel, 0, "operation_name", schedule, es, sampler=sampler)

        es.assert_not_called()

    def test_execute_single_no_return_value(self):
        es = None
        params = None
        runner = mock.Mock()

        total_ops, total_ops_unit, request_meta_data = driver.execute_single(self.context_managed(runner), es, params)

        self.assertEqual(1, total_ops)
        self.assertEqual("ops", total_ops_unit)
        self.assertEqual({"success": True}, request_meta_data)

    def test_execute_single_tuple(self):
        es = None
        params = None
        runner = mock.Mock()
        runner.return_value = (500, "MB")

        total_ops, total_ops_unit, request_meta_data = driver.execute_single(self.context_managed(runner), es, params)

        self.assertEqual(500, total_ops)
        self.assertEqual("MB", total_ops_unit)
        self.assertEqual({"success": True}, request_meta_data)

    def test_execute_single_dict(self):
        es = None
        params = None
        runner = mock.Mock()
        runner.return_value = {
            "weight": 50,
            "unit": "docs",
            "some-custom-meta-data": "valid",
            "http-status": 200
        }

        total_ops, total_ops_unit, request_meta_data = driver.execute_single(self.context_managed(runner), es, params)

        self.assertEqual(50, total_ops)
        self.assertEqual("docs", total_ops_unit)
        self.assertEqual({
            "some-custom-meta-data": "valid",
            "http-status": 200,
            "success": True
        }, request_meta_data)

    def test_execute_single_with_connection_error(self):
        import elasticsearch
        es = None
        params = None
        # ES client uses pseudo-status "N/A" in this case...
        runner = mock.Mock(side_effect=elasticsearch.ConnectionError("N/A", "no route to host"))

        total_ops, total_ops_unit, request_meta_data = driver.execute_single(self.context_managed(runner), es, params)

        self.assertEqual(0, total_ops)
        self.assertEqual("ops", total_ops_unit)
        self.assertEqual({
            # Look ma: No http-status!
            "error-description": "no route to host",
            "success": False
        }, request_meta_data)

    def test_execute_single_with_http_400(self):
        import elasticsearch
        es = None
        params = None
        runner = mock.Mock(side_effect=elasticsearch.NotFoundError(404, "not found"))

        total_ops, total_ops_unit, request_meta_data = driver.execute_single(self.context_managed(runner), es, params)

        self.assertEqual(0, total_ops)
        self.assertEqual("ops", total_ops_unit)
        self.assertEqual({
            "http-status": 404,
            "error-description": "not found",
            "success": False
        }, request_meta_data)

    def test_execute_single_with_key_error(self):
        class FailingRunner:
            def __call__(self, *args):
                raise KeyError("bulk-size missing")

            def __str__(self):
                return "failing_mock_runner"

        es = None
        params = collections.OrderedDict()
        # simulating an error; this should be "bulk-size"
        params["bulk"] = 5000
        params["mode"] = "append"
        runner = FailingRunner()

        with self.assertRaises(exceptions.SystemSetupError) as ctx:
            driver.execute_single(self.context_managed(runner), es, params)
        self.assertEqual(
            "Cannot execute [failing_mock_runner]. Provided parameters are: ['bulk', 'mode']. Error: ['bulk-size missing'].",
            ctx.exception.args[0])


class ClusterHealthCheckTests(TestCase):
    @mock.patch("elasticsearch.Elasticsearch")
    def test_waits_for_expected_cluster_status(self, es):
        es.info.return_value = {
            "version": {
                "number": "6.0.0"
            }
        }
        es.cluster.health.return_value = {
            "status": "green",
            "relocating_shards": 0
        }

        reached_cluster_status, relocating_shards = driver._do_wait(es, "green")

        self.assertEqual("green", reached_cluster_status)
        self.assertEqual(0, relocating_shards)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_accepts_better_cluster_status(self, es):
        es.info.return_value = {
            "version": {
                "number": "6.0.0"
            }
        }
        es.cluster.health.return_value = {
            "status": "green",
            "relocating_shards": 0
        }

        reached_cluster_status, relocating_shards = driver._do_wait(es, "yellow")

        self.assertEqual("green", reached_cluster_status)
        self.assertEqual(0, relocating_shards)

    @mock.patch("elasticsearch.Elasticsearch")
    def test_rejects_relocating_shards(self, es):
        es.info.return_value = {
            "version": {
                "number": "6.0.0"
            }
        }
        es.cluster.health.return_value = {
            "status": "yellow",
            "relocating_shards": 3
        }

        with self.assertRaises(exceptions.RallyAssertionError) as ctx:
            driver._do_wait(es, "red", sleep=lambda t: t)
        self.assertEqual("Cluster reached status [yellow] which is equal or better than the expected status [red] but there were [3] "
                         "relocating shards and we require zero relocating shards (Use the /_cat/shards API to check which shards are "
                         "relocating.)", ctx.exception.args[0])

    @mock.patch("elasticsearch.Elasticsearch")
    def test_rejects_unknown_cluster_status(self, es):
        es.info.return_value = {
            "version": {
                "number": "6.0.0"
            }
        }
        es.cluster.health.return_value = {
            "status": None,
            "relocating_shards": 0
        }

        with self.assertRaises(exceptions.RallyAssertionError) as ctx:
            driver._do_wait(es, "red", sleep=lambda t: t)
        self.assertEqual("Cluster did not reach status [red]. Last reached status: [None]", ctx.exception.args[0])
