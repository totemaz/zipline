import blaze as bz
import itertools
from nose.tools import assert_true
from nose_parameterized import parameterized
import numpy as np
import pandas as pd
from pandas.util.testing import assert_frame_equal
from toolz import merge

from zipline.pipeline import SimplePipelineEngine, Pipeline, CustomFactor
from zipline.pipeline.common import (
    EVENT_DATE_FIELD_NAME,
    FISCAL_QUARTER_FIELD_NAME,
    FISCAL_YEAR_FIELD_NAME,
    SID_FIELD_NAME,
    TS_FIELD_NAME,
)
from zipline.pipeline.data import DataSet
from zipline.pipeline.data import Column
from zipline.pipeline.loaders.blaze.estimates import (
    BlazeNextEstimatesLoader,
    BlazePreviousEstimatesLoader
)
from zipline.pipeline.loaders.quarter_estimates import (
    NextQuartersEstimatesLoader,
    normalize_quarters,
    PreviousQuartersEstimatesLoader,
    split_normalized_quarters,
)
from zipline.testing.fixtures import (
    WithAssetFinder,
    WithTradingSessions,
    ZiplineTestCase,
)
from zipline.testing.predicates import assert_equal
from zipline.utils.numpy_utils import datetime64ns_dtype
from zipline.utils.numpy_utils import float64_dtype


class Estimates(DataSet):
    event_date = Column(dtype=datetime64ns_dtype)
    fiscal_quarter = Column(dtype=float64_dtype)
    fiscal_year = Column(dtype=float64_dtype)
    estimate = Column(dtype=float64_dtype)


def QuartersEstimates(num_qtr):
    class QtrEstimates(Estimates):
        num_quarters = num_qtr
        name = Estimates
    return QtrEstimates


class WithEstimates(WithTradingSessions, WithAssetFinder):
    """
    ZiplineTestCase mixin providing cls.loader and cls.events as class
    level fixtures.


    Methods
    -------
    make_loader(events, columns) -> PipelineLoader
        Method which returns the loader to be used throughout tests.

        events : pd.DataFrame
            The raw events to be used as input to the pipeline loader.
        columns : dict[str -> str]
            The dictionary mapping the names of BoundColumns to the
            associated column name in the events DataFrame.
    """

    # Short window defined in order for test to run faster.
    START_DATE = pd.Timestamp('2014-12-28')
    END_DATE = pd.Timestamp('2015-02-03')

    @classmethod
    def make_loader(cls, events, columns):
        raise NotImplementedError('make_loader')

    @classmethod
    def init_class_fixtures(cls):
        cls.sids = cls.events[SID_FIELD_NAME].unique()
        cls.columns = {
            Estimates.event_date: 'event_date',
            Estimates.fiscal_quarter: 'fiscal_quarter',
            Estimates.fiscal_year: 'fiscal_year',
            Estimates.estimate: 'estimate'
        }
        cls.loader = cls.make_loader(cls.events, {column.name: val for
                                                  column, val in
                                                  cls.columns.items()})
        cls.ASSET_FINDER_EQUITY_SIDS = list(
            cls.events[SID_FIELD_NAME].unique()
        )
        cls.ASSET_FINDER_EQUITY_SYMBOLS = [
            's' + str(n) for n in cls.ASSET_FINDER_EQUITY_SIDS
        ]
        super(WithEstimates, cls).init_class_fixtures()


class WithWrongNumQuarters(WithEstimates):
    """
    ZiplineTestCase mixin providing cls.events as a class level fixture and
    defining a test for all inheritors to use.

    Attributes
    ----------
    events : pd.DataFrame
        A simple DataFrame with columns needed for estimates and a single sid
        and no other data.

    Tests
    ------
    test_wrong_num_quarters_passed()
        Tests that loading with an incorrect quarter number raises an error.
    """
    events = pd.DataFrame({SID_FIELD_NAME: 0},
                          columns=[SID_FIELD_NAME,
                                   TS_FIELD_NAME,
                                   EVENT_DATE_FIELD_NAME,
                                   FISCAL_QUARTER_FIELD_NAME,
                                   FISCAL_YEAR_FIELD_NAME,
                                   'estimate'],
                          index=[0])

    def test_wrong_num_quarters_passed(self):
        dataset = QuartersEstimates(-1)
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )
        p = Pipeline({c.name: c.latest for c in dataset.columns})

        with self.assertRaises(ValueError):
            engine.run_pipeline(
                p,
                start_date=self.trading_days[0],
                end_date=self.trading_days[-1],
            )


class PreviousWithWrongNumQuarters(WithWrongNumQuarters,
                                   ZiplineTestCase):
    """
    Tests that previous quarter loader correctly breaks if an incorrect
    number of quarters is passed.
    """
    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)


class NextWithWrongNumQuarters(WithWrongNumQuarters,
                               ZiplineTestCase):
    """
    Tests that next quarter loader correctly breaks if an incorrect
    number of quarters is passed.
    """
    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)


class WithEstimatesT0(WithEstimates):
    """
    ZiplineTestCase mixin providing cls.events as a class level fixture and
    defining a test for all inheritors to use.

    Attributes
    ----------
    cls.events : pd.DataFrame
        Generated dynamically in order to test inter-leavings of estimates and
        event dates for multiple quarters to make sure that we select the
        right immediate 'next' or 'previous' quarter relative to each date -
        i.e., the right 't0' on the timeline. We care about selecting the
        right 't0' because we use that to calculate which quarter's data needs
        to be returned for each day.

    Methods
    -------
    get_expected_estimate(q1_knowledge,
                          q2_knowledge,
                          comparable_date) -> pd.DataFrame
        Retrieves the expected estimate given the latest knowledge about each
        quarter and the date on which the estimate is being requested. If
        there is no expected estimate, returns an empty DataFrame.

    Tests
    ------
    test_estimates()
        Tests that we get the right 't0' value on each day for each sid and
        for each column.
    """
    q1_knowledge_dates = [pd.Timestamp('2015-01-01'),
                          pd.Timestamp('2015-01-04'),
                          pd.Timestamp('2015-01-08'),
                          pd.Timestamp('2015-01-12')]
    q2_knowledge_dates = [pd.Timestamp('2015-01-16'),
                          pd.Timestamp('2015-01-20'),
                          pd.Timestamp('2015-01-24'),
                          pd.Timestamp('2015-01-28')]
    # We want to model the possibility of an estimate predicting a release date
    # that doesn't match the actual release. This could be done by dynamically
    # generating more combinations with different release dates, but that
    # significantly increases the amount of time it takes to run the tests.
    # These hard-coded cases are sufficient to know that we can update our
    # beliefs when we get new information.
    q1_release_dates = [pd.Timestamp('2015-01-15'),
                        pd.Timestamp('2015-01-16')]  # One day late
    q2_release_dates = [pd.Timestamp('2015-01-30'),  # One day early
                        pd.Timestamp('2015-01-31')]

    @classmethod
    def gen_estimates(cls):
        """
        In order to determine which estimate we care about for a particular
        sid, we need to look at all estimates that we have for that sid and
        their associated event dates.

        We define q1 < q2, and thus event1 < event2 since event1 occurs
        during q1 and event2 occurs during q2 and we assume that there can
        only be 1 event per quarter. We assume that there can be multiple
        estimates per quarter leading up to the event. We assume that estimates
        will not surpass the relevant event date. We will look at 2 estimates
        for an event before the event occurs, since that is the simplest
        scenario that covers the interesting edge cases:
            - estimate values changing
            - a release date changing
            - estimates for different quarters interleaving

        Thus, we generate all possible inter-leavings of 2 estimates per
        quarter-event where estimate1 < estimate2 and all estimates are < the
        relevant event and assign each of these inter-leavings to a
        different sid.
        """

        sid_estimates = []
        sid_releases = []
        # We want all permutations of 2 knowledge dates per quarter.
        it = enumerate(
            itertools.permutations(cls.q1_knowledge_dates +
                                   cls.q2_knowledge_dates,
                                   4)
        )
        for sid, (q1e1, q1e2, q2e1, q2e2) in it:
            # We're assuming that estimates must come before the relevant
            # release.
            if (q1e1 < q1e2 and
                    q2e1 < q2e2 and
                    # All estimates are < Q2's event, so just constrain Q1
                    # estimates.
                    q1e1 < cls.q1_release_dates[0] and
                    q1e2 < cls.q1_release_dates[0]):
                sid_estimates.append(cls.create_estimates_df(q1e1,
                                                             q1e2,
                                                             q2e1,
                                                             q2e2,
                                                             sid))
                sid_releases.append(cls.create_releases_df(sid))

        return pd.concat(sid_estimates + sid_releases).reset_index(drop=True)

    @classmethod
    def create_releases_df(cls, sid):
        # Final release dates never change. The quarters have very tight date
        # ranges in order to reduce the number of dates we need to iterate
        # through when testing.
        return pd.DataFrame({
            TS_FIELD_NAME: [pd.Timestamp('2015-01-15'),
                            pd.Timestamp('2015-01-31')],
            EVENT_DATE_FIELD_NAME: [pd.Timestamp('2015-01-15'),
                                    pd.Timestamp('2015-01-31')],
            'estimate': [0.5, 0.8],
            FISCAL_QUARTER_FIELD_NAME: [1.0, 2.0],
            FISCAL_YEAR_FIELD_NAME: [2015.0, 2015.0],
            SID_FIELD_NAME: sid
        })

    @classmethod
    def create_estimates_df(cls,
                            q1e1,
                            q1e2,
                            q2e1,
                            q2e2,
                            sid):
        return pd.DataFrame({
            EVENT_DATE_FIELD_NAME: cls.q1_release_dates + cls.q2_release_dates,
            'estimate': [.1, .2, .3, .4],
            FISCAL_QUARTER_FIELD_NAME: [1.0, 1.0, 2.0, 2.0],
            FISCAL_YEAR_FIELD_NAME: [2015.0, 2015.0, 2015.0, 2015.0],
            TS_FIELD_NAME: [q1e1, q1e2, q2e1, q2e2],
            SID_FIELD_NAME: sid,
        })

    @classmethod
    def init_class_fixtures(cls):
        # Must be generated before call to super since super uses `events`.
        cls.events = cls.gen_estimates()
        super(WithEstimatesT0, cls).init_class_fixtures()

    def get_expected_estimate(self,
                              q1_knowledge,
                              q2_knowledge,
                              comparable_date):
        return pd.DataFrame()

    def test_estimates(self):
        dataset = QuartersEstimates(1)
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )
        results = engine.run_pipeline(
            Pipeline({c.name: c.latest for c in dataset.columns}),
            start_date=self.trading_days[0],
            end_date=self.trading_days[-1],
        )
        for sid in self.sids:
            sid_estimates = results.xs(sid, level=1)
            ts_sorted_estimates = self.events[
                self.events[SID_FIELD_NAME] == sid
            ].sort(TS_FIELD_NAME)
            for i, date in enumerate(sid_estimates.index):
                comparable_date = date.tz_localize(None)
                # Filter out estimates we don't know about yet.
                ts_eligible_estimates = ts_sorted_estimates[
                    ts_sorted_estimates[TS_FIELD_NAME] <= comparable_date
                ]
                # If there are estimates we know about:
                if not ts_eligible_estimates.empty:
                    # Determine the last piece of information we know about
                    # for q1 and q2. This takes advantage of the fact that we
                    # only have 2 quarters in the test data.
                    q1_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 1
                    ]
                    q2_knowledge = ts_eligible_estimates[
                        ts_eligible_estimates[FISCAL_QUARTER_FIELD_NAME] == 2
                    ]
                    expected_estimate = self.get_expected_estimate(
                        q1_knowledge,
                        q2_knowledge,
                        comparable_date,
                    )
                    if not expected_estimate.empty:
                        for colname in sid_estimates.columns:
                            expected_value = expected_estimate[colname]
                            computed_value = sid_estimates.iloc[i][colname]
                            assert_equal(expected_value, computed_value)
                    else:
                        # There are no eligible 'next' estimates on this day;
                        #  everything should be null.
                        assert_true(sid_estimates.iloc[i].isnull().all())
                else:
                    # We don't know about any estimates on this day;
                    # everything should be null.
                    assert_true(sid_estimates.iloc[i].isnull().all())


class NextEstimate(WithEstimatesT0, ZiplineTestCase):
    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)

    def get_expected_estimate(self,
                              q1_knowledge,
                              q2_knowledge,
                              comparable_date):
        # If our latest knowledge of q1 is that the release is
        # happening on this simulation date or later, then that's
        # the estimate we want to use.
        if (not q1_knowledge.empty and
            q1_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] >=
                comparable_date):
            return q1_knowledge.iloc[-1]
        # If q1 has already happened or we don't know about it
        # yet and our latest knowledge indicates that q2 hasn't
        # happened yet, then that's the estimate we want to use.
        elif (not q2_knowledge.empty and
              q2_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] >=
                comparable_date):
            return q2_knowledge.iloc[-1]
        return pd.DataFrame()


class BlazeNextEstimateLoaderTestCase(NextEstimate):
    """
    Run the same tests as EventsLoaderTestCase, but using a BlazeEventsLoader.
    """

    @classmethod
    def make_loader(cls, events, columns):
        return BlazeNextEstimatesLoader(
            bz.data(events),
            columns,
        )


class PreviousEstimate(WithEstimatesT0, ZiplineTestCase):
    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)

    def get_expected_estimate(self,
                              q1_knowledge,
                              q2_knowledge,
                              comparable_date):

        # The expected estimate will be for q2 if the last thing
        # we've seen is that the release date already happened.
        # Otherwise, it'll be for q1, as long as the release date
        # for q1 has already happened.
        if (not q2_knowledge.empty and
            q2_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] <=
                comparable_date):
            return q2_knowledge.iloc[-1]
        elif (not q1_knowledge.empty and
              q1_knowledge.iloc[-1][EVENT_DATE_FIELD_NAME] <=
                comparable_date):
            return q1_knowledge.iloc[-1]
        return pd.DataFrame()


class BlazePreviousEstimateLoaderTestCase(PreviousEstimate):
    """
    Run the same tests as EventsLoaderTestCase, but using a BlazeEventsLoader.
    """

    @classmethod
    def make_loader(cls, events, columns):
        return BlazePreviousEstimatesLoader(
            bz.data(events),
            columns,
        )


class WithEstimateMultipleQuarters(WithEstimates):
    """
    ZiplineTestCase mixin providing cls.events, cls.make_expected_out as
    class-level fixtures and self.test_multiple_qtrs_requested as a test.

    Attributes
    ----------
    events : pd.DataFrame
        Simple DataFrame with estimates for 2 quarters for a single sid.

    Methods
    -------
    make_expected_out() --> pd.DataFrame
        Returns the DataFrame that is expected as a result of running a
        Pipeline where estimates are requested for multiple quarters out.
    fill_expected_out(expected)
        Fills the expected DataFrame with data.

    Tests
    ------
    test_multiple_qtrs_requested()
        Runs a Pipeline that calculate which estimates for multiple quarters
        out and checks that the returned columns contain data for the correct
        number of quarters out.
    """
    events = pd.DataFrame({
        SID_FIELD_NAME: [0] * 2,
        TS_FIELD_NAME: [pd.Timestamp('2015-01-01'),
                        pd.Timestamp('2015-01-06')],
        EVENT_DATE_FIELD_NAME: [pd.Timestamp('2015-01-10'),
                                pd.Timestamp('2015-01-20')],
        'estimate': [1., 2.],
        FISCAL_QUARTER_FIELD_NAME: [1, 2],
        FISCAL_YEAR_FIELD_NAME: [2015, 2015]
    })

    @classmethod
    def init_class_fixtures(cls):
        super(WithEstimateMultipleQuarters, cls).init_class_fixtures()
        cls.expected_out = cls.make_expected_out()

    @classmethod
    def make_expected_out(cls):
        expected = pd.DataFrame(columns=[cls.columns[col] + '1'
                                         for col in cls.columns] +
                                        [cls.columns[col] + '2'
                                         for col in cls.columns],
                                index=cls.trading_days)

        for (col, raw_name), suffix in itertools.product(
            cls.columns.items(), ('1', '2')
        ):
            expected_name = raw_name + suffix
            if col.dtype == datetime64ns_dtype:
                expected[expected_name] = pd.to_datetime(
                    expected[expected_name]
                )
            else:
                expected[expected_name] = expected[
                    expected_name
                ].astype(col.dtype)
        cls.fill_expected_out(expected)
        return expected.reindex(cls.trading_days)

    def test_multiple_qtrs_requested(self):
        dataset1 = QuartersEstimates(1)
        dataset2 = QuartersEstimates(2)
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )

        results = engine.run_pipeline(
            Pipeline(
                merge([{c.name + '1': c.latest for c in dataset1.columns},
                       {c.name + '2': c.latest for c in dataset2.columns}])
            ),
            start_date=self.trading_days[0],
            end_date=self.trading_days[-1],
        )
        q1_columns = [col.name + '1' for col in self.columns]
        q2_columns = [col.name + '2' for col in self.columns]

        # We now expect a column for 1 quarter out and a column for 2
        # quarters out for each of the dataset columns.
        assert_equal(sorted(np.array(q1_columns + q2_columns)),
                     sorted(results.columns.values))
        assert_frame_equal(self.expected_out.sort(axis=1),
                           results.xs(0, level=1).sort(axis=1))


class NextEstimateMultipleQuarters(
    WithEstimateMultipleQuarters, ZiplineTestCase
):
    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)

    @classmethod
    def fill_expected_out(cls, expected):
        # Fill columns for 1 Q out
        for raw_name in cls.columns.values():
            expected[raw_name + '1'].loc[
                pd.Timestamp('2015-01-01'):pd.Timestamp('2015-01-11')
            ] = cls.events[raw_name].iloc[0]
            expected[raw_name + '1'].loc[
                pd.Timestamp('2015-01-11'):pd.Timestamp('2015-01-20')
            ] = cls.events[raw_name].iloc[1]

        # Fill columns for 2 Q out
        # We only have an estimate and event date for 2 quarters out before
        # Q1's event happens; after Q1's event, we know 1 Q out but not 2 Qs
        # out.
        for col_name in ['estimate', 'event_date']:
            expected[col_name + '2'].loc[
                pd.Timestamp('2015-01-06'):pd.Timestamp('2015-01-10')
            ] = cls.events[col_name].iloc[1]
        # But we know what FQ and FY we'd need in both Q1 and Q2
        # because we know which FQ is next and can calculate from there
        expected[FISCAL_QUARTER_FIELD_NAME + '2'].loc[
            pd.Timestamp('2015-01-01'):pd.Timestamp('2015-01-09')
        ] = 2
        expected[FISCAL_QUARTER_FIELD_NAME + '2'].loc[
            pd.Timestamp('2015-01-12'):pd.Timestamp('2015-01-20')
        ] = 3
        expected[FISCAL_YEAR_FIELD_NAME + '2'].loc[
            pd.Timestamp('2015-01-01'):pd.Timestamp('2015-01-20')
        ] = 2015

        return expected


class PreviousEstimateMultipleQuarters(
    WithEstimateMultipleQuarters,
    ZiplineTestCase
):

    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)

    @classmethod
    def fill_expected_out(cls, expected):
        # Fill columns for 1 Q out
        for raw_name in cls.columns.values():
            expected[raw_name + '1'].loc[
                pd.Timestamp('2015-01-12'):pd.Timestamp('2015-01-19')
            ] = cls.events[raw_name].iloc[0]
            expected[raw_name + '1'].loc[
                pd.Timestamp('2015-01-20'):
            ] = cls.events[raw_name].iloc[1]

        # Fill columns for 2 Q out
        for col_name in ['estimate', 'event_date']:
            expected[col_name + '2'].loc[
                pd.Timestamp('2015-01-20'):
            ] = cls.events[col_name].iloc[0]
        expected[
            FISCAL_QUARTER_FIELD_NAME + '2'
        ].loc[pd.Timestamp('2015-01-12'):pd.Timestamp('2015-01-20')] = 4
        expected[
            FISCAL_YEAR_FIELD_NAME + '2'
        ].loc[pd.Timestamp('2015-01-12'):pd.Timestamp('2015-01-20')] = 2014
        expected[
            FISCAL_QUARTER_FIELD_NAME + '2'
        ].loc[pd.Timestamp('2015-01-20'):] = 1
        expected[
            FISCAL_YEAR_FIELD_NAME + '2'
        ].loc[pd.Timestamp('2015-01-20'):] = 2015
        return expected


class WithEstimateWindows(WithEstimates):
    """
    ZiplineTestCase mixin providing fixures and a test to test running a
    Pipeline with an estimates loader over differently-sized windows.

    Attributes
    ----------
    events : pd.DataFrame
        DataFrame with estimates for 2 quarters for 2 sids.
    window_test_start_date : pd.Timestamp
        The date from which the window should start.
    timelines : dict[int -> pd.DataFrame]
        A dictionary mapping to the number of quarters out to
        snapshots of how the data should look on each date in the date range.

    Methods
    -------
    make_expected_timelines() -> dict[int -> pd.DataFrame]
        Creates a dictionary of expected data. See `timelines`, above.

    Tests
    -----
    test_estimate_windows_at_quarter_boundaries()
        Tests that we overwrite values with the correct quarter's estimate at
        the correct dates when we have a factor that asks for a window of data.
    """
    sid_0_timeline = pd.DataFrame({
        TS_FIELD_NAME: [pd.Timestamp('2015-01-05'),
                        pd.Timestamp('2015-01-07'),
                        pd.Timestamp('2015-01-05'),
                        pd.Timestamp('2015-01-17')],
        EVENT_DATE_FIELD_NAME:
            [pd.Timestamp('2015-01-10'),
             pd.Timestamp('2015-01-10'),
             pd.Timestamp('2015-01-20'),
             pd.Timestamp('2015-01-20')],
        'estimate': [10., 11.] + [20., 21.],
        FISCAL_QUARTER_FIELD_NAME: [1] * 2 + [2] * 2,
        FISCAL_YEAR_FIELD_NAME: 2015,
        SID_FIELD_NAME: 0,
    })

    sid_1_timeline = pd.DataFrame({
        TS_FIELD_NAME: [pd.Timestamp('2015-01-09'),
                        pd.Timestamp('2015-01-12'),
                        pd.Timestamp('2015-01-09'),
                        pd.Timestamp('2015-01-15')],
        EVENT_DATE_FIELD_NAME:
            [pd.Timestamp('2015-01-12'), pd.Timestamp('2015-01-12'),
             pd.Timestamp('2015-01-15'), pd.Timestamp('2015-01-15')],
        'estimate': [10., 11.] + [30., 31.],
        FISCAL_QUARTER_FIELD_NAME: [1] * 2 + [3] * 2,
        FISCAL_YEAR_FIELD_NAME: 2015,
        SID_FIELD_NAME: 1
    })

    window_test_start_date = pd.Timestamp('2015-01-05')
    critical_dates = [pd.Timestamp('2015-01-09', tz='utc'),
                      pd.Timestamp('2015-01-12', tz='utc'),
                      pd.Timestamp('2015-01-15', tz='utc'),
                      pd.Timestamp('2015-01-20', tz='utc')]
    # window length, starting date, num quarters out, timeline. Parameterizes
    # over number of quarters out.
    window_test_cases = list(itertools.product(critical_dates, (1, 2)))
    events = pd.concat([sid_0_timeline, sid_1_timeline])

    @classmethod
    def make_expected_timelines(cls):
        return {}

    @classmethod
    def init_class_fixtures(cls):
        super(WithEstimateWindows, cls).init_class_fixtures()
        cls.timelines = cls.make_expected_timelines()

    @classmethod
    def create_expected_df(cls, tuples, end_date):
        """
        Given a list of tuples of new data we get for each sid on each critical
        date (when information changes), create a DataFrame that fills that
        data through a date range ending at `end_date`.
        """
        df = pd.DataFrame(tuples,
                          columns=[SID_FIELD_NAME,
                                   'estimate',
                                   'knowledge_date'])
        df = df.pivot_table(columns='sid',
                            values='estimate',
                            index='knowledge_date')
        df = df.reindex(
            pd.date_range(cls.window_test_start_date, end_date)
        )
        # Index name is lost during reindex.
        df.index = df.index.rename('knowledge_date')
        df['at_date'] = end_date.tz_localize('utc')
        df = df.set_index(['at_date', df.index.tz_localize('utc')]).ffill()
        return df

    @parameterized.expand(window_test_cases)
    def test_estimate_windows_at_quarter_boundaries(self,
                                                    start_idx,
                                                    num_quarters_out):
        dataset = QuartersEstimates(num_quarters_out)
        trading_days = self.trading_days
        timelines = self.timelines
        # The window length should be from the starting index back to the first
        # date on which we got data. The goal is to ensure that as we
        # progress through the timeline, all data we got, starting from that
        # first date, is correctly overwritten.
        window_len = (
            self.trading_days.get_loc(start_idx) -
            self.trading_days.get_loc(self.window_test_start_date) + 1
        )

        class SomeFactor(CustomFactor):
            inputs = [dataset.estimate]
            window_length = window_len

            def compute(self, today, assets, out, estimate):
                today_idx = trading_days.get_loc(today)
                today_timeline = timelines[
                    num_quarters_out
                ].loc[today].reindex(
                    trading_days[:today_idx + 1]
                ).values
                timeline_start_idx = (len(today_timeline) - window_len)
                assert_equal(estimate,
                             today_timeline[timeline_start_idx:])
        engine = SimplePipelineEngine(
            lambda x: self.loader,
            self.trading_days,
            self.asset_finder,
        )
        engine.run_pipeline(
            Pipeline({'est': SomeFactor()}),
            start_date=start_idx,
            end_date=pd.Timestamp('2015-01-20', tz='utc'),  # last event date
            # we have
        )


class PreviousEstimateWindows(WithEstimateWindows, ZiplineTestCase):
    @classmethod
    def make_loader(cls, events, columns):
        return PreviousQuartersEstimatesLoader(events, columns)

    @classmethod
    def make_expected_timelines(cls):
        oneq_previous = pd.concat([
            cls.create_expected_df(
                [(0, np.NaN, cls.window_test_start_date),
                 (1, np.NaN, cls.window_test_start_date)],
                pd.Timestamp('2015-01-09')
            ),
            cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-10')),
                 (1, 11, pd.Timestamp('2015-01-12'))],
                pd.Timestamp('2015-01-12')
            ),
            cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-10')),
                 (1, 11, pd.Timestamp('2015-01-12'))],
                pd.Timestamp('2015-01-13')
            ),
            cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-10')),
                 (1, 11, pd.Timestamp('2015-01-12'))],
                pd.Timestamp('2015-01-14')
            ),
            cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-10')),
                 (1, 31, pd.Timestamp('2015-01-15'))],
                pd.Timestamp('2015-01-15')
            ),
            cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-10')),
                 (1, 31, pd.Timestamp('2015-01-15'))],
                pd.Timestamp('2015-01-16')
            ),
            cls.create_expected_df(
                [(0, 21, pd.Timestamp('2015-01-17')),
                 (1, 31, pd.Timestamp('2015-01-15'))],
                pd.Timestamp('2015-01-20')
            ),
        ])

        twoq_previous = pd.concat(
            [cls.create_expected_df(
                [(0, np.NaN, cls.window_test_start_date),
                 (1, np.NaN, cls.window_test_start_date)],
                end_date
            ) for end_date in pd.date_range('2015-01-09', '2015-01-19')] +
            [cls.create_expected_df(
                [(0, 11, pd.Timestamp('2015-01-20')),
                 (1, np.NaN, cls.window_test_start_date)],
                pd.Timestamp('2015-01-20')
            )]
        )
        return {
            1: oneq_previous,
            2: twoq_previous
        }


class NextEstimateWindows(WithEstimateWindows, ZiplineTestCase):
    @classmethod
    def make_loader(cls, events, columns):
        return NextQuartersEstimatesLoader(events, columns)

    @classmethod
    def make_expected_timelines(cls):
        oneq_next = pd.concat([
            cls.create_expected_df(
                [(0, 10, cls.window_test_start_date),
                 (0, 11, pd.Timestamp('2015-01-07')),
                 (1, 10, pd.Timestamp('2015-01-09'))],
                pd.Timestamp('2015-01-09')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (1, 10, pd.Timestamp('2015-01-09')),
                 (1, 11, pd.Timestamp('2015-01-12'))],
                pd.Timestamp('2015-01-12')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (1, 30, pd.Timestamp('2015-01-09'))],
                pd.Timestamp('2015-01-13')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (1, 30, pd.Timestamp('2015-01-09'))],
                pd.Timestamp('2015-01-14')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (1, 30, pd.Timestamp('2015-01-09')),
                 (1, 31, pd.Timestamp('2015-01-15'))],
                pd.Timestamp('2015-01-15')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (1, np.NaN, cls.window_test_start_date)],
                pd.Timestamp('2015-01-16')
            ),
            cls.create_expected_df(
                [(0, 20, cls.window_test_start_date),
                 (0, 21, pd.Timestamp('2015-01-17')),
                 (1, np.NaN, cls.window_test_start_date)],
                pd.Timestamp('2015-01-20')
            ),
        ])

        twoq_next = pd.concat(
            [cls.create_expected_df(
                [(0, 20, pd.Timestamp(cls.window_test_start_date)),
                 (1, np.NaN, pd.Timestamp(cls.window_test_start_date))],
                pd.Timestamp('2015-01-09')
            )] +
            [cls.create_expected_df(
                [(0, np.NaN, pd.Timestamp(cls.window_test_start_date)),
                 (1, np.NaN, pd.Timestamp(cls.window_test_start_date))],
                end_date
            ) for end_date in pd.date_range('2015-01-12', '2015-01-20')]
        )

        return {
            1: oneq_next,
            2: twoq_next
        }


class QuarterShiftTestCase(ZiplineTestCase):
    """
    This tests, in isolation, quarter calculation logic for shifting quarters
    backwards/forwards from a starting point.
    """
    def test_quarter_normalization(self):
        input_yrs = pd.Series([0] * 4)
        input_qtrs = pd.Series(range(1, 5))
        result_years, result_quarters = split_normalized_quarters(
            normalize_quarters(input_yrs, input_qtrs)
        )
        # Can't use assert_series_equal here with check_names=False
        # because that still fails due to name differences.
        assert_equal(input_yrs, result_years)
        assert_equal(input_qtrs, result_quarters)
