from StringIO import StringIO
from nose.tools import (
    eq_,
    set_trace,
)
import datetime
import pkgutil
import csv
from copy import deepcopy

from metadata_layer import (
    CSVFormatError,
    CSVMetadataImporter,
    CirculationData,
    ContributorData,
    MeasurementData,
    FormatData,
    LinkData,
    Metadata,
    IdentifierData,
    ReplacementPolicy,
    SubjectData,
    ContributorData,
)

import os
from model import (
    CoverageRecord,
    DataSource,
    Edition,
    Identifier,
    Measurement,
    DeliveryMechanism,
    Hyperlink, 
    Representation,
    Subject,
)

from . import (
    DatabaseTest,
    DummyHTTPClient,
)

from s3 import DummyS3Uploader
from classifier import NO_VALUE, NO_NUMBER

class TestIdentifierData(object):

    def test_constructor(self):
        data = IdentifierData(Identifier.ISBN, "foo", 0.5)
        eq_(Identifier.ISBN, data.type)
        eq_("foo", data.identifier)
        eq_(0.5, data.weight)

class TestMetadataImporter(DatabaseTest):

    def test_parse(self):
        base_path = os.path.split(__file__)[0]
        path = os.path.join(
            base_path, "files/csv/staff_picks_small.csv")
        reader = csv.DictReader(open(path))
        importer = CSVMetadataImporter(
            DataSource.LIBRARY_STAFF,
        )
        generator = importer.to_metadata(reader)
        m1, m2, m3 = list(generator)

        eq_(u"Horrorst\xf6r", m1.title)
        eq_("Grady Hendrix", m1.contributors[0].display_name)
        eq_("Martin Jensen", m2.contributors[0].display_name)

        # Let's check out the identifiers we found.

        # The first book has an Overdrive ID
        [overdrive] = m1.identifiers
        eq_(Identifier.OVERDRIVE_ID, overdrive.type)
        eq_('504BA8F6-FF4E-4B57-896E-F1A50CFFCA0C', overdrive.identifier)
        eq_(0.75, overdrive.weight)

        # The second book has no ID at all.
        eq_([], m2.identifiers)

        # The third book has both a 3M ID and an Overdrive ID.
        overdrive, threem = sorted(m3.identifiers, key=lambda x: x.identifier)

        eq_(Identifier.OVERDRIVE_ID, overdrive.type)
        eq_('eae60d41-e0b8-4f9d-90b5-cbc43d433c2f', overdrive.identifier)
        eq_(0.75, overdrive.weight)

        eq_(Identifier.THREEM_ID, threem.type)
        eq_('eswhyz9', threem.identifier)
        eq_(0.75, threem.weight)

        # Now let's check out subjects.
        eq_(
            [
                ('schema:typicalAgeRange', u'Adult', 100),
                ('tag', u'Character Driven', 100),
                ('tag', u'Historical', 100), 
                ('tag', u'Nail-Biters', 100),
                ('tag', u'Setting Driven', 100)
            ],
            [(x.type, x.identifier, x.weight) 
             for x in sorted(m2.subjects, key=lambda x: x.identifier)]
        )

    def test_classifications_from_another_source_not_updated(self):

        # Set up an edition whose primary identifier has two
        # classifications.
        source1 = DataSource.lookup(self._db, DataSource.AXIS_360)
        source2 = DataSource.lookup(self._db, DataSource.METADATA_WRANGLER)
        edition = self._edition()
        identifier = edition.primary_identifier
        c1 = identifier.classify(source1, Subject.TAG, "i will persist")
        c2 = identifier.classify(source2, Subject.TAG, "i will perish")

        # Now we get some new metadata from source #2.
        subjects = [SubjectData(type=Subject.TAG, identifier="i will conquer")]
        metadata = Metadata(subjects=subjects, data_source=source2)
        replace = ReplacementPolicy(subjects=True)
        metadata.apply(edition, replace=replace)

        # The old classification from source #2 has been destroyed.
        # The old classification from source #1 is still there.
        eq_(
            ['i will conquer', 'i will persist'],
            sorted([x.subject.identifier for x in identifier.classifications])
        )

    def test_links(self):
        edition = self._edition()
        l1 = LinkData(rel=Hyperlink.IMAGE, href="http://example.com/")
        l2 = LinkData(rel=Hyperlink.DESCRIPTION, content="foo")
        metadata = Metadata(links=[l1, l2], 
                            data_source=edition.data_source)
        metadata.apply(edition)
        [image, description] = sorted(
            edition.primary_identifier.links, key=lambda x:x.rel
        )
        eq_(Hyperlink.IMAGE, image.rel)
        eq_("http://example.com/", image.resource.url)

        eq_(Hyperlink.DESCRIPTION, description.rel)
        eq_("foo", description.resource.representation.content)

    def test_image_and_thumbnail(self):
        edition = self._edition()
        l2 = LinkData(
            rel=Hyperlink.THUMBNAIL_IMAGE, href="http://thumbnail.com/",
            media_type=Representation.JPEG_MEDIA_TYPE,
        )
        l1 = LinkData(
            rel=Hyperlink.IMAGE, href="http://example.com/", thumbnail=l2,
            media_type=Representation.JPEG_MEDIA_TYPE,
        )
        metadata = Metadata(links=[l1, l2], 
                            data_source=edition.data_source)
        metadata.apply(edition)
        [image, thumbnail] = sorted(
            edition.primary_identifier.links, key=lambda x:x.rel
        )
        eq_(Hyperlink.IMAGE, image.rel)
        eq_([thumbnail.resource.representation],
            image.resource.representation.thumbnails
        )

    def sample_cover_path(self, name):
        base_path = os.path.split(__file__)[0]
        resource_path = os.path.join(base_path, "files", "covers")
        sample_cover_path = os.path.join(resource_path, name)
        return sample_cover_path


    def test_image_scale_and_mirror(self):
        # Make sure that open access material links are translated to our S3 buckets, and that 
        # commercial material links are left as is.
        # Note: mirroring links is now also CirculationData's job.  So the unit tests 
        # that test for that have been changed to call to mirror cover images.
        # However, updated tests passing does not guarantee that all code now 
        # correctly calls on CirculationData, too.  This is a risk.

        mirror = DummyS3Uploader()
        edition, pool = self._edition(with_license_pool=True)
        content = open(self.sample_cover_path("test-book-cover.png")).read()
        l1 = LinkData(
            rel=Hyperlink.IMAGE, href="http://example.com/",
            media_type=Representation.JPEG_MEDIA_TYPE,
            content=content
        )
        thumbnail_content = open(self.sample_cover_path("tiny-image-cover.png")).read()
        l2 = LinkData(
            rel=Hyperlink.THUMBNAIL_IMAGE, href="http://example.com/thumb.jpg",
            media_type=Representation.JPEG_MEDIA_TYPE,
            content=content
        )

        # When we call metadata.apply, all image links will be scaled and
        # 'mirrored'.
        policy = ReplacementPolicy(mirror=mirror)
        metadata = Metadata(links=[l1, l2], data_source=edition.data_source)
        metadata.apply(edition, replace=policy)

        # Two Representations were 'mirrored'.
        image, thumbnail = mirror.uploaded

        # The image...
        [image_link] = image.resource.links
        eq_(Hyperlink.IMAGE, image_link.rel)

        # And its thumbnail.
        eq_(image, thumbnail.thumbnail_of)

        # The original image is too big to be a thumbnail.
        eq_(600, image.image_height)
        eq_(400, image.image_width)

        # The thumbnail is the right height.
        eq_(Edition.MAX_THUMBNAIL_HEIGHT, thumbnail.image_height)
        eq_(Edition.MAX_THUMBNAIL_WIDTH, thumbnail.image_width)

        # The thumbnail is newly generated from the full-size
        # image--the thumbnail that came in from the OPDS feed was
        # ignored.
        assert thumbnail.url != l2.href
        assert thumbnail.content != l2.content

        # Both images have been 'mirrored' to Amazon S3.
        assert image.mirror_url.startswith('http://s3.amazonaws.com/test.cover.bucket/')
        assert image.mirror_url.endswith('cover.jpg')

        # The thumbnail image has been converted to PNG.
        assert thumbnail.mirror_url.startswith('http://s3.amazonaws.com/test.cover.bucket/scaled/300/')
        assert thumbnail.mirror_url.endswith('cover.png')


    def test_mirror_open_access_link_fetch_failure(self):
        edition, pool = self._edition(with_license_pool=True)

        data_source = DataSource.lookup(self._db, DataSource.GUTENBERG)
        m = Metadata(data_source=data_source)

        mirror = DummyS3Uploader()
        h = DummyHTTPClient()

        policy = ReplacementPolicy(mirror=mirror, http_get=h.do_get)

        link = LinkData(
            rel=Hyperlink.IMAGE,
            media_type=Representation.JPEG_MEDIA_TYPE,
            href="http://example.com/",
        )

        link_obj, ignore = edition.primary_identifier.add_link(
            rel=link.rel, href=link.href, data_source=data_source,
            license_pool=pool, media_type=link.media_type,
            content=link.content,
        )
        h.queue_response(403)
        
        m.mirror_link(edition, data_source, link, link_obj, policy)

        representation = link_obj.resource.representation

        # Fetch failed, so we should have a fetch exception but no mirror url.
        assert representation.fetch_exception != None
        eq_(None, representation.mirror_exception)
        eq_(None, representation.mirror_url)
        eq_(link.href, representation.url)
        assert representation.fetched_at != None
        eq_(None, representation.mirrored_at)

        # the edition's identifier-associated license pool should not be 
        # suppressed just because fetch failed on getting image.
        eq_(False, pool.suppressed)

        # the license pool only gets its license_exception column filled in
        # if fetch failed on getting an Hyperlink.OPEN_ACCESS_DOWNLOAD-type epub.
        eq_(None, pool.license_exception)


    def test_mirror_open_access_link_mirror_failure(self):
        edition, pool = self._edition(with_license_pool=True)

        data_source = DataSource.lookup(self._db, DataSource.GUTENBERG)
        m = Metadata(data_source=data_source)

        mirror = DummyS3Uploader(fail=True)
        h = DummyHTTPClient()

        policy = ReplacementPolicy(mirror=mirror, http_get=h.do_get)

        content = open(self.sample_cover_path("test-book-cover.png")).read()
        link = LinkData(
            rel=Hyperlink.IMAGE,
            media_type=Representation.JPEG_MEDIA_TYPE,
            href="http://example.com/",
            content=content
        )

        link_obj, ignore = edition.primary_identifier.add_link(
            rel=link.rel, href=link.href, data_source=data_source,
            license_pool=pool, media_type=link.media_type,
            content=link.content,
        )

        h.queue_response(200, media_type=Representation.JPEG_MEDIA_TYPE)
        
        m.mirror_link(edition, data_source, link, link_obj, policy)

        representation = link_obj.resource.representation

        # The representation was fetched successfully.
        eq_(None, representation.fetch_exception)
        assert representation.fetched_at != None

        # But mirroing failed.
        assert representation.mirror_exception != None
        eq_(None, representation.mirrored_at)
        eq_(link.media_type, representation.media_type)
        eq_(link.href, representation.url)

        # The mirror url should still be set.
        assert "Gutenberg" in representation.mirror_url
        assert representation.mirror_url.endswith("%s/cover.jpg" % edition.primary_identifier.identifier)

        # Book content is still there since it wasn't mirrored.
        assert representation.content != None

        # the edition's identifier-associated license pool should not be 
        # suppressed just because fetch failed on getting image.
        eq_(False, pool.suppressed)

        # the license pool only gets its license_exception column filled in
        # if fetch failed on getting an Hyperlink.OPEN_ACCESS_DOWNLOAD-type epub.
        eq_(None, pool.license_exception)


    def test_measurements(self):
        edition = self._edition()
        measurement = MeasurementData(quantity_measured=Measurement.POPULARITY,
                                      value=100)
        metadata = Metadata(measurements=[measurement],
                            data_source=edition.data_source)
        metadata.apply(edition)
        [m] = edition.primary_identifier.measurements
        eq_(Measurement.POPULARITY, m.quantity_measured)
        eq_(100, m.value)


    def test_coverage_record(self):
        edition, pool = self._edition(with_license_pool=True)
        data_source = edition.data_source

        # No preexisting coverage record
        coverage = CoverageRecord.lookup(edition, data_source)
        eq_(coverage, None)
        
        last_update = datetime.datetime(2015, 1, 1)

        m = Metadata(data_source=data_source,
                     title=u"New title", provider_entry_updated=last_update)
        m.apply(edition)
        
        coverage = CoverageRecord.lookup(edition, data_source)
        eq_(last_update, coverage.timestamp)
        eq_(u"New title", edition.title)

        older_last_update = datetime.datetime(2014, 1, 1)
        m = Metadata(data_source=data_source,
                     title=u"Another new title", 
                     provider_entry_updated=older_last_update
        )
        m.apply(edition)
        eq_(u"New title", edition.title)

        coverage = CoverageRecord.lookup(edition, data_source)
        eq_(last_update, coverage.timestamp)

        m.apply(edition, force=True)
        eq_(u"Another new title", edition.title)
        coverage = CoverageRecord.lookup(edition, data_source)
        eq_(older_last_update, coverage.timestamp)



class TestContributorData(DatabaseTest):
    def test_from_contribution(self):
        # Makes sure ContributorData.from_contribution copies all the fields over.
        
        # make author with that name, add author to list and pass to edition
        contributors = ["PrimaryAuthor"]
        edition, pool = self._edition(with_license_pool=True, authors=contributors)
        
        contribution = edition.contributions[0]
        contributor = contribution.contributor
        contributor.lc = "1234567"
        contributor.viaf = "ABC123"
        contributor.aliases = ["Primo"]
        contributor.display_name = "Test Author For The Win"
        contributor.family_name = "TestAuttie"
        contributor.wikipedia_name = "TestWikiAuth"
        contributor.biography = "He was born on Main Street."

        contributor_data = ContributorData.from_contribution(contribution)

        # make sure contributor fields are still what I expect
        eq_(contributor_data.lc, contributor.lc)
        eq_(contributor_data.viaf, contributor.viaf)
        eq_(contributor_data.aliases, contributor.aliases)
        eq_(contributor_data.display_name, contributor.display_name)
        eq_(contributor_data.family_name, contributor.family_name)
        eq_(contributor_data.wikipedia_name, contributor.wikipedia_name)
        eq_(contributor_data.biography, contributor.biography)


class TestMetadata(DatabaseTest):
    def test_from_edition(self):
        # Makes sure Metadata.from_edition copies all the fields over.

        edition, pool = self._edition(with_license_pool=True)
        edition.series = "Harry Otter and the Mollusk of Infamy"
        edition.series_position = "14"
        metadata = Metadata.from_edition(edition)

        # make sure the metadata and the originating edition match 
        for field in Metadata.BASIC_EDITION_FIELDS:
            eq_(getattr(edition, field), getattr(metadata, field))


        e_contribution = edition.contributions[0]
        m_contributor_data = metadata.contributors[0]
        eq_(e_contribution.contributor.name, m_contributor_data.sort_name)
        eq_(e_contribution.role, m_contributor_data.roles[0])

        eq_(edition.data_source, metadata.data_source(self._db))
        eq_(edition.primary_identifier.identifier, metadata.primary_identifier.identifier)

    def test_update(self):
        # Tests that Metadata.update correctly prefers new fields to old, unless 
        # new fields aren't defined.

        edition_old, pool = self._edition(with_license_pool=True)
        edition_old.publisher = "test_old_publisher"
        edition_old.subtitle = "old_subtitile"
        metadata_old = Metadata.from_edition(edition_old)

        edition_new, pool = self._edition(with_license_pool=True)
        # set more fields on metadatas
        edition_new.publisher = None
        edition_new.subtitle = "new_updated_subtitile"
        metadata_new = Metadata.from_edition(edition_new)

        metadata_old.update(metadata_new)

        eq_(metadata_old.publisher, "test_old_publisher")
        eq_(metadata_old.subtitle, metadata_new.subtitle)

    def test_apply(self):
        edition_old, pool = self._edition(with_license_pool=True)

        metadata = Metadata(
            data_source=DataSource.OVERDRIVE,
            title=u"The Harry Otter and the Seaweed of Ages",
            sort_title=u"Harry Otter and the Seaweed of Ages, The",
            subtitle=u"Kelp At It",
            series=u"The Harry Otter Sagas",
            series_position=u"4",
            language=u"eng",
            medium=u"Audio",
            publisher=u"Scholastic Inc",
            imprint=u"Follywood",
            published=datetime.date(1987, 5, 4),
            issued=datetime.date(1989, 4, 5)
        )

        edition_new, changed = metadata.apply(edition_old)

        eq_(changed, True)
        eq_(edition_new.title, u"The Harry Otter and the Seaweed of Ages")
        eq_(edition_new.sort_title, u"Harry Otter and the Seaweed of Ages, The")
        eq_(edition_new.subtitle, u"Kelp At It")
        eq_(edition_new.series, u"The Harry Otter Sagas")
        eq_(edition_new.series_position, u"4")
        eq_(edition_new.language, u"eng")
        eq_(edition_new.medium, u"Audio")
        eq_(edition_new.publisher, u"Scholastic Inc")
        eq_(edition_new.imprint, u"Follywood")
        eq_(edition_new.published, datetime.date(1987, 5, 4))
        eq_(edition_new.issued, datetime.date(1989, 4, 5))

        edition_new, changed = metadata.apply(edition_new)
        eq_(changed, False)

    def test_apply_identifier_equivalency(self):

        # Set up primary identifier with matching & new IdentifierData objects
        edition, pool = self._edition(with_license_pool=True)
        primary = edition.primary_identifier
        primary_as_data = IdentifierData(
            type=primary.type, identifier=primary.identifier
        )
        other_data = IdentifierData(type=u"abc", identifier=u"def")

        # Prep Metadata object.
        metadata = Metadata(
            data_source=DataSource.OVERDRIVE,
            primary_identifier=primary,
            identifiers=[primary_as_data, other_data]
        )

        # The primary identifier is put into the identifiers array after init
        eq_(3, len(metadata.identifiers))
        assert primary in metadata.identifiers

        metadata.apply(edition)
        # Neither the primary edition nor the identifier data that represents
        # it have become equivalencies.
        eq_(1, len(primary.equivalencies))
        [equivalency] = primary.equivalencies
        eq_(equivalency.output.type, u"abc")
        eq_(equivalency.output.identifier, u"def")

    def test_apply_no_value(self):
        edition_old, pool = self._edition(with_license_pool=True)

        metadata = Metadata(
            data_source=DataSource.PRESENTATION_EDITION,
            subtitle=NO_VALUE,
            series=NO_VALUE,
            series_position=NO_NUMBER
        )

        edition_new, changed = metadata.apply(edition_old)

        eq_(changed, True)
        eq_(edition_new.title, edition_old.title)
        eq_(edition_new.sort_title, edition_old.sort_title)
        eq_(edition_new.subtitle, None)
        eq_(edition_new.series, None)
        eq_(edition_new.series_position, None)
        eq_(edition_new.language, edition_old.language)
        eq_(edition_new.medium, edition_old.medium)
        eq_(edition_new.publisher, edition_old.publisher)
        eq_(edition_new.imprint, edition_old.imprint)
        eq_(edition_new.published, edition_old.published)
        eq_(edition_new.issued, edition_old.issued)


    def test_metadata_can_be_deepcopied(self):
        # Check that we didn't put something in the metadata that
        # will prevent it from being copied. (e.g., self.log)

        subject = SubjectData(Subject.TAG, "subject")
        contributor = ContributorData()
        identifier = IdentifierData(Identifier.GUTENBERG_ID, "1")
        link = LinkData(Hyperlink.OPEN_ACCESS_DOWNLOAD, "example.epub")
        measurement = MeasurementData(Measurement.RATING, 5)
        circulation = CirculationData(data_source=DataSource.GUTENBERG,
            primary_identifier=identifier, 
            licenses_owned=0, 
            licenses_available=0, 
            licenses_reserved=0, 
            patrons_in_hold_queue=0)
        primary_as_data = IdentifierData(
            type=identifier.type, identifier=identifier.identifier
        )
        other_data = IdentifierData(type=u"abc", identifier=u"def")

        m = Metadata(
            DataSource.GUTENBERG,
            subjects=[subject],
            contributors=[contributor],
            primary_identifier=identifier,
            links=[link],
            measurements=[measurement],
            circulation=circulation,

            title="Hello Title",
            subtitle="Subtle Hello",
            sort_title="Sorting Howdy",
            language="US English",
            medium=Edition.BOOK_MEDIUM,
            series="1",
            series_position=1,
            publisher="Hello World Publishing House",
            imprint=u"Follywood",
            issued=datetime.datetime.utcnow(),
            published=datetime.datetime.utcnow(),
            identifiers=[primary_as_data, other_data],
            provider_entry_updated=datetime.datetime.utcnow(),
        )

        m_copy = deepcopy(m)

        # If deepcopy didn't throw an exception we're ok.
        assert m_copy is not None


    def test_links_filtered(self):
        # test that filter links to only metadata-relevant ones
        link1 = LinkData(Hyperlink.OPEN_ACCESS_DOWNLOAD, "example.epub")
        link2 = LinkData(rel=Hyperlink.IMAGE, href="http://example.com/")
        link3 = LinkData(rel=Hyperlink.DESCRIPTION, content="foo")
        link4 = LinkData(
            rel=Hyperlink.THUMBNAIL_IMAGE, href="http://thumbnail.com/",
            media_type=Representation.JPEG_MEDIA_TYPE,
        )
        link5 = LinkData(
            rel=Hyperlink.IMAGE, href="http://example.com/", thumbnail=link4,
            media_type=Representation.JPEG_MEDIA_TYPE,
        )
        links = [link1, link2, link3, link4, link5]

        identifier = IdentifierData(Identifier.GUTENBERG_ID, "1")
        metadata = Metadata(
            DataSource.GUTENBERG,
            primary_identifier=identifier,
            links=links,
        )

        filtered_links = sorted(metadata.links, key=lambda x:x.rel)

        eq_([link2, link5, link4, link3], filtered_links)










