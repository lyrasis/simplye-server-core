# encoding: utf-8
from nose.tools import (
    assert_raises_regexp,
    eq_,
    set_trace,
)
import os
import json
import pkgutil

from overdrive import (
    OverdriveAPI,
    MockOverdriveAPI,
    OverdriveRepresentationExtractor,
)

from model import (
    Contributor,
    DeliveryMechanism,
    Edition,
    Identifier,
    Representation,
    Subject,
    Measurement,
    Hyperlink,
)

from util.http import (
    BadResponseException,
    HTTP,
)

from . import DatabaseTest


class TestOverdriveAPI(DatabaseTest):

    def setup(self):
        super(TestOverdriveAPI, self).setup()
        self.api = MockOverdriveAPI(self._db)

    def test_make_link_safe(self):
        eq_("http://foo.com?q=%2B%3A%7B%7D",
            OverdriveAPI.make_link_safe("http://foo.com?q=+:{}"))

    def test_token_post_success(self):
        self.api.queue_response(200, content="some content")
        response = self.api.token_post(self._url, "the payload")
        eq_(200, response.status_code)
        eq_("some content", response.content)

    def test_get_success(self):
        self.api.queue_response(200, content="some content")
        status_code, headers, content = self.api.get(self._url, {})
        eq_(200, status_code)
        eq_("some content", content)

    def test_failure_to_get_library_is_fatal(self):
        # We already called get_library while initializing the
        # Overdrive API, and when that happened we cached its
        # Representation. Delete the Representation so we stop using
        # the cached version.
        for r in self._db.query(Representation):
            self._db.delete(r)
        self._db.commit()

        self.api.queue_response(500)
        assert_raises_regexp(
            BadResponseException, 
            ".*Got status code 500.*",
            self.api.get_library
        )

    def test_401_on_get_refreshes_bearer_token(self):

        eq_("bearer token", self.api.token)

        # We try to GET and receive a 401.
        self.api.queue_response(401)

        # We refresh the bearer token.
        self.api.queue_response(
            200, content=self.api.mock_access_token("new bearer token")
        )

        # Then we retry the GET and it succeeds this time.
        self.api.queue_response(200, content="at last, the content")

        status_code, headers, content = self.api.get(self._url, {})

        eq_(200, status_code)
        eq_("at last, the content", content)

        # The bearer token has been updated.
        eq_("new bearer token", self.api.token)

    def test_credential_refresh_success(self):
        """Verify the process of refreshing the Overdrive bearer token.
        """
        credential = self.api.credential_object(lambda x: x)
        eq_("bearer token", credential.credential)
        eq_(self.api.token, credential.credential)

        self.api.queue_response(
            200, content=self.api.mock_access_token("new bearer token")
        )

        self.api.refresh_creds(credential)
        eq_("new bearer token", credential.credential)
        eq_(self.api.token, credential.credential)

    def test_401_after_token_refresh_raises_error(self):

        eq_("bearer token", self.api.token)

        # We try to GET and receive a 401.
        self.api.queue_response(401)

        # We refresh the bearer token.
        self.api.queue_response(
            200, content=self.api.mock_access_token("new bearer token")
        )

        # Then we retry the GET but we get another 401.
        self.api.queue_response(401)

        # That raises a BadResponseException
        assert_raises_regexp(
            BadResponseException, "Bad response from .*:Something's wrong with the Overdrive OAuth Bearer Token!",
        )

    def test_401_during_refresh_raises_error(self):
        """If we fail to refresh the OAuth bearer token, an exception is
        raised.
        """
        self.api.queue_response(401)

        assert_raises_regexp(
            BadResponseException,
            ".*Got status code 401.*can only continue on: 200.",        
            self.api.refresh_creds,
            None
        )


class TestOverdriveRepresentationExtractor(object):

    def setup(self):
        base_path = os.path.split(__file__)[0]
        self.resource_path = os.path.join(base_path, "files", "overdrive")

    def sample_json(self, filename):
        path = os.path.join(self.resource_path, filename)
        data = open(path).read()
        return data, json.loads(data)

    def test_availability_info(self):
        data, raw = self.sample_json("overdrive_book_list.json")
        availability = OverdriveRepresentationExtractor.availability_link_list(
            raw)
        for item in availability:
            for key in 'availability_link', 'id', 'title':
                assert key in item

    def test_link(self):
        data, raw = self.sample_json("overdrive_book_list.json")
        expect = OverdriveAPI.make_link_safe("http://api.overdrive.com/v1/collections/collection-id/products?limit=300&offset=0&lastupdatetime=2014-04-28%2009:25:09&sort=popularity:desc&formats=ebook-epub-open,ebook-epub-adobe,ebook-pdf-adobe,ebook-pdf-open")
        eq_(expect, OverdriveRepresentationExtractor.link(raw, "first"))


    def test_book_info_with_circulationdata(self):
        # Tests that can convert an overdrive json block into a CirculationData object.

        raw, info = self.sample_json("overdrive_availability_information.json")
        circulationdata = OverdriveRepresentationExtractor.book_info_to_circulation(info)

        # Related IDs.
        eq_((Identifier.OVERDRIVE_ID, '2a005d55-a417-4053-b90d-7a38ca6d2065'),
            (circulationdata.primary_identifier.type, circulationdata.primary_identifier.identifier))


    def test_book_info_with_metadata(self):
        # Tests that can convert an overdrive json block into a Metadata object.

        raw, info = self.sample_json("overdrive_metadata.json")
        metadata = OverdriveRepresentationExtractor.book_info_to_metadata(info)

        eq_("Agile Documentation", metadata.title)
        eq_("Agile Documentation A Pattern Guide to Producing Lightweight Documents for Software Projects", metadata.sort_title)
        eq_("A Pattern Guide to Producing Lightweight Documents for Software Projects", metadata.subtitle)
        eq_(Edition.BOOK_MEDIUM, metadata.medium)
        eq_("Wiley Software Patterns", metadata.series)
        eq_("eng", metadata.language)
        eq_("Wiley", metadata.publisher)
        eq_("John Wiley & Sons, Inc.", metadata.imprint)
        eq_(2005, metadata.published.year)
        eq_(1, metadata.published.month)
        eq_(31, metadata.published.day)

        [author] = metadata.contributors
        eq_(u"Rüping, Andreas", author.sort_name)
        eq_("Andreas R&#252;ping", author.display_name)
        eq_([Contributor.AUTHOR_ROLE], author.roles)

        subjects = sorted(metadata.subjects, key=lambda x: x.identifier)

        eq_([("Computer Technology", Subject.OVERDRIVE, 100),
             ("Nonfiction", Subject.OVERDRIVE, 100),
             ('Object Technologies - Miscellaneous', 'tag', 1),
         ],
            [(x.identifier, x.type, x.weight) for x in subjects]
        )

        # Related IDs.
        eq_((Identifier.OVERDRIVE_ID, '3896665d-9d81-4cac-bd43-ffc5066de1f5'),
            (metadata.primary_identifier.type, metadata.primary_identifier.identifier))

        ids = [(x.type, x.identifier) for x in metadata.identifiers]

        # The original data contains a blank ASIN in addition to the
        # actual ASIN, but it doesn't show up here.
        eq_(
            [
                (Identifier.ASIN, "B000VI88N2"), 
                (Identifier.ISBN, "9780470856246"),
                (Identifier.OVERDRIVE_ID, '3896665d-9d81-4cac-bd43-ffc5066de1f5'),
            ],
            sorted(ids)
        )

        # Available formats.      
        [kindle, pdf] = sorted(metadata.circulation.formats, key=lambda x: x.content_type)        
        eq_(DeliveryMechanism.KINDLE_CONTENT_TYPE, kindle.content_type)       
        eq_(DeliveryMechanism.KINDLE_DRM, kindle.drm_scheme)      

        eq_(Representation.PDF_MEDIA_TYPE, pdf.content_type)      
        eq_(DeliveryMechanism.ADOBE_DRM, pdf.drm_scheme)

        # Links to various resources.
        shortd, image, longd = sorted(
            metadata.links, key=lambda x:x.rel
        )

        eq_(Hyperlink.DESCRIPTION, longd.rel)
        assert longd.content.startswith("<p>Software documentation")

        eq_(Hyperlink.SHORT_DESCRIPTION, shortd.rel)
        assert shortd.content.startswith("<p>Software documentation")
        assert len(shortd.content) < len(longd.content)

        eq_(Hyperlink.IMAGE, image.rel)
        eq_('http://images.contentreserve.com/ImageType-100/0128-1/%7B3896665D-9D81-4CAC-BD43-FFC5066DE1F5%7DImg100.jpg', image.href)

        thumbnail = image.thumbnail

        eq_(Hyperlink.THUMBNAIL_IMAGE, thumbnail.rel)
        eq_('http://images.contentreserve.com/ImageType-200/0128-1/%7B3896665D-9D81-4CAC-BD43-FFC5066DE1F5%7DImg200.jpg', thumbnail.href)

        # Measurements associated with the book.

        measurements = metadata.measurements
        popularity = [x for x in measurements
                      if x.quantity_measured==Measurement.POPULARITY][0]
        eq_(2, popularity.value)

        rating = [x for x in measurements
                  if x.quantity_measured==Measurement.RATING][0]
        eq_(1, rating.value)


    def test_book_info_with_sample(self):
        raw, info = self.sample_json("has_sample.json")
        metadata = OverdriveRepresentationExtractor.book_info_to_metadata(info)
        [sample] = [x for x in metadata.links if x.rel == Hyperlink.SAMPLE]
        eq_("http://excerpts.contentreserve.com/FormatType-410/1071-1/9BD/24F/82/BridesofConvenienceBundle9781426803697.epub", sample.href)

    def test_book_info_with_grade_levels(self):
        raw, info = self.sample_json("has_grade_levels.json")
        metadata = OverdriveRepresentationExtractor.book_info_to_metadata(info)

        grade_levels = sorted(
            [x.identifier for x in metadata.subjects 
             if x.type==Subject.GRADE_LEVEL]
        )
        eq_([u'Grade 4', u'Grade 5', u'Grade 6', u'Grade 7', u'Grade 8'],
            grade_levels)

    def test_book_info_with_awards(self):
        raw, info = self.sample_json("has_awards.json")
        metadata = OverdriveRepresentationExtractor.book_info_to_metadata(info)

        [awards] = [x for x in metadata.measurements 
                    if Measurement.AWARDS == x.quantity_measured
        ]
        eq_(1, awards.value)
        eq_(1, awards.weight)
