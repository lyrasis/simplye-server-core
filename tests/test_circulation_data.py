from nose.tools import (
    eq_,
    set_trace,
)

from copy import deepcopy
import datetime

from metadata_layer import (
    CirculationData,
    ContributorData,
    FormatData,
    IdentifierData,
    LinkData,
    Metadata,
    ReplacementPolicy,
    SubjectData,
)

from model import (
    DataSource,
    DeliveryMechanism,
    Hyperlink, 
    Identifier,
    Representation,
    RightsStatus,
    Subject,
)

from . import (
    DatabaseTest,
    DummyHTTPClient,
)

from s3 import DummyS3Uploader


class TestCirculationData(DatabaseTest):

    def setup(self):
        super(TestCirculationData, self).setup()
        self.collection = self._collection()
        
    def test_circulationdata_can_be_deepcopied(self):
        # Check that we didn't put something in the CirculationData that
        # will prevent it from being copied. (e.g., self.log)

        subject = SubjectData(Subject.TAG, "subject")
        contributor = ContributorData()
        identifier = IdentifierData(Identifier.GUTENBERG_ID, "1")
        link = LinkData(Hyperlink.OPEN_ACCESS_DOWNLOAD, "example.epub")
        format = FormatData(Representation.EPUB_MEDIA_TYPE, DeliveryMechanism.NO_DRM)
        rights_uri = RightsStatus.GENERIC_OPEN_ACCESS

        circulation_data = CirculationData(
            DataSource.GUTENBERG,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
            licenses_owned=5,
            licenses_available=5,
            licenses_reserved=None,
            patrons_in_hold_queue=None,
            formats=[format],
            default_rights_uri=rights_uri,
        )

        circulation_data_copy = deepcopy(circulation_data)

        # If deepcopy didn't throw an exception we're ok.
        assert circulation_data_copy is not None


    def test_links_filtered(self):
        # Tests that passed-in links filter down to only the relevant ones.
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
        circulation_data = CirculationData(
            DataSource.GUTENBERG,
            primary_identifier=identifier,
            collection=self.collection,
            links=links,
        )

        filtered_links = sorted(circulation_data.links, key=lambda x:x.rel)

        eq_([link1], filtered_links)


    def test_explicit_formatdata(self):
        # Creating an edition with an open-access download will
        # automatically create a delivery mechanism.
        edition, pool = self._edition(with_open_access_download=True)

        # Let's also add a DRM format.
        drm_format = FormatData(
            content_type=Representation.PDF_MEDIA_TYPE,
            drm_scheme=DeliveryMechanism.ADOBE_DRM,
        )

        circulation_data = CirculationData(
            formats=[drm_format],
            data_source=edition.data_source, 
            primary_identifier=edition.primary_identifier,
            collection=self.collection,
        )
        circulation_data.apply(pool)

        [epub, pdf] = sorted(pool.delivery_mechanisms, 
                             key=lambda x: x.delivery_mechanism.content_type)
        eq_(epub.resource, edition.license_pool.best_open_access_resource)

        eq_(Representation.PDF_MEDIA_TYPE, pdf.delivery_mechanism.content_type)
        eq_(DeliveryMechanism.ADOBE_DRM, pdf.delivery_mechanism.drm_scheme)

        # If we tell Metadata to replace the list of formats, we only
        # have the one format we manually created.
        replace = ReplacementPolicy(
                formats=True,
            )
        circulation_data.apply(pool, replace=replace)
        [pdf] = pool.delivery_mechanisms
        eq_(Representation.PDF_MEDIA_TYPE, pdf.delivery_mechanism.content_type)

    def test_apply_removes_old_formats_based_on_replacement_policy(self):
        edition, pool = self._edition(with_license_pool=True)

        # Start with one delivery mechanism for this pool.
        for lpdm in pool.delivery_mechanisms:
            self._db.delete(lpdm)
        pool.delivery_mechanisms = []

        old_lpdm = pool.set_delivery_mechanism(
            Representation.PDF_MEDIA_TYPE, DeliveryMechanism.ADOBE_DRM,
            RightsStatus.IN_COPYRIGHT, None)

        # And it has been loaned.
        patron = self._patron()
        loan, ignore = pool.loan_to(patron, fulfillment=old_lpdm)
        eq_(old_lpdm, loan.fulfillment)

        # We have new circulation data that has a different format.
        format = FormatData(
            content_type=Representation.EPUB_MEDIA_TYPE,
            drm_scheme=DeliveryMechanism.ADOBE_DRM,
        )
        circulation_data = CirculationData(
            formats=[format],
            data_source=edition.data_source,
            primary_identifier=edition.primary_identifier,
            collection=self.collection,
        )

        # If we apply the new CirculationData with formats false in the policy,
        # we'll add the new format, but keep the old one as well.
        replacement_policy = ReplacementPolicy(formats=False)
        circulation_data.apply(pool, replacement_policy)
        
        eq_(2, len(pool.delivery_mechanisms))
        eq_(set([Representation.PDF_MEDIA_TYPE, Representation.EPUB_MEDIA_TYPE]),
            set([lpdm.delivery_mechanism.content_type for lpdm in pool.delivery_mechanisms]))
        eq_(old_lpdm, loan.fulfillment)

        # But if we make formats true in the policy, we'll delete the old format
        # and remove it from its loan.
        replacement_policy = ReplacementPolicy(formats=True)
        circulation_data.apply(pool, replacement_policy)

        eq_(1, len(pool.delivery_mechanisms))
        eq_(Representation.EPUB_MEDIA_TYPE, pool.delivery_mechanisms[0].delivery_mechanism.content_type)
        eq_(None, loan.fulfillment)
        

    def test_license_pool_sets_default_license_values(self):
        """We have no information about how many copies of the book we've
        actually licensed, but a LicensePool can be created anyway,
        so we can store format information.
        """
        identifier = IdentifierData(Identifier.OVERDRIVE_ID, "1")
        drm_format = FormatData(
            content_type=Representation.PDF_MEDIA_TYPE,
            drm_scheme=DeliveryMechanism.ADOBE_DRM,
        )
        circulation = CirculationData(
            data_source=DataSource.OVERDRIVE,
            primary_identifier=identifier,
            collection=self.collection,
            formats=[drm_format],
        )
        pool, is_new = circulation.license_pool(
            self._db,
        )
        eq_(True, is_new)

        # We start with the conservative assumption that we own no
        # licenses for the book.
        eq_(0, pool.licenses_owned)
        eq_(0, pool.licenses_available)
        eq_(0, pool.licenses_reserved)
        eq_(0, pool.patrons_in_hold_queue)

    def test_implicit_format_for_open_access_link(self):
        # A format is a delivery mechanism.  We handle delivery on open access 
        # pools from our mirrored content in S3.  
        # Tests that when a link is open access, a pool can be delivered.
        
        edition, pool = self._edition(with_license_pool=True)

        # This is the delivery mechanism created by default when you
        # create a book with _edition().
        [epub] = pool.delivery_mechanisms
        eq_(Representation.EPUB_MEDIA_TYPE, epub.delivery_mechanism.content_type)
        eq_(DeliveryMechanism.ADOBE_DRM, epub.delivery_mechanism.drm_scheme)


        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.PDF_MEDIA_TYPE,
            href=self._url
        )
        circulation_data = CirculationData(
            data_source=DataSource.GUTENBERG, 
            primary_identifier=edition.primary_identifier,
            collection=self.collection,
            links=[link], 
        )

        replace = ReplacementPolicy(
                formats=True,
            )
        circulation_data.apply(pool, replace)

        # We destroyed the default delivery format and added a new,
        # open access delivery format.
        [pdf] = pool.delivery_mechanisms
        eq_(Representation.PDF_MEDIA_TYPE, pdf.delivery_mechanism.content_type)
        eq_(DeliveryMechanism.NO_DRM, pdf.delivery_mechanism.drm_scheme)

        circulation_data = CirculationData(
            data_source=DataSource.GUTENBERG, 
            primary_identifier=edition.primary_identifier,
            collection=self.collection,
            links=[]
        )
        replace = ReplacementPolicy(
                formats=True,
                links=True,
            )
        circulation_data.apply(pool, replace)

        # Now we have no formats at all.
        eq_([], pool.delivery_mechanisms)

    def test_rights_status_default_rights_passed_in(self):
        identifier = IdentifierData(
            Identifier.GUTENBERG_ID,
            "abcd",
        )
        link = LinkData(
            rel=Hyperlink.DRM_ENCRYPTED_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url
        )

        circulation_data = CirculationData(
            data_source=DataSource.OA_CONTENT_SERVER,
            primary_identifier=identifier,
            default_rights_uri = RightsStatus.CC_BY,
            collection=self.collection,
            links=[link],
        )

        replace = ReplacementPolicy(
            formats=True,
        )

        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(True, pool.open_access)
        eq_(1, len(pool.delivery_mechanisms))
        # The rights status is the one that was passed in to CirculationData.
        eq_(RightsStatus.CC_BY, pool.delivery_mechanisms[0].rights_status.uri)

    def test_rights_status_default_rights_from_data_source(self):
        identifier = IdentifierData(
            Identifier.GUTENBERG_ID,
            "abcd",
        )
        link = LinkData(
            rel=Hyperlink.DRM_ENCRYPTED_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url
        )

        circulation_data = CirculationData(
            data_source=DataSource.OA_CONTENT_SERVER,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
        )

        replace = ReplacementPolicy(
            formats=True,
        )

        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(True, pool.open_access)
        eq_(1, len(pool.delivery_mechanisms))
        # The rights status is the default for the OA content server.
        eq_(RightsStatus.GENERIC_OPEN_ACCESS, pool.delivery_mechanisms[0].rights_status.uri)

    def test_rights_status_open_access_link_no_rights_uses_data_source_default(self):
        identifier = IdentifierData(
            Identifier.GUTENBERG_ID,
            "abcd",
        )
        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url
        )

        circulation_data = CirculationData(
            data_source=DataSource.GUTENBERG,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
        )
        replace = ReplacementPolicy(
            formats=True,
        )

        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(True, pool.open_access)
        eq_(1, len(pool.delivery_mechanisms))

        # The delivery mechanism's rights status is the default for
        # the data source.
        eq_(RightsStatus.PUBLIC_DOMAIN_USA, pool.delivery_mechanisms[0].rights_status.uri)

        # Even if a commercial source like Overdrive should offer a
        # link with rel="open access", unless we know it's an
        # open-access link we will give it a RightsStatus of
        # IN_COPYRIGHT.
        identifier = IdentifierData(
            Identifier.OVERDRIVE_ID,
            "abcd",
        )

        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url
        )

        circulation_data = CirculationData(
            data_source=DataSource.OVERDRIVE,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
        )
        
        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(RightsStatus.IN_COPYRIGHT,
            pool.delivery_mechanisms[0].rights_status.uri)

        # This will cause the work to be treated as a non-open-access
        # work.
        eq_(False, pool.open_access)

        
    def test_rights_status_open_access_link_with_rights(self):
        identifier = IdentifierData(
            Identifier.OVERDRIVE_ID,
            "abcd",
        )
        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url,
            rights_uri=RightsStatus.CC_BY_ND,
        )

        circulation_data = CirculationData(
            data_source=DataSource.OVERDRIVE,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
        )
        replace = ReplacementPolicy(
            formats=True,
        )

        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(True, pool.open_access)
        eq_(1, len(pool.delivery_mechanisms))
        eq_(RightsStatus.CC_BY_ND, pool.delivery_mechanisms[0].rights_status.uri)

    def test_rights_status_commercial_link_with_rights(self):
        identifier = IdentifierData(
            Identifier.OVERDRIVE_ID,
            "abcd",
        )
        link = LinkData(
            rel=Hyperlink.DRM_ENCRYPTED_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url,
            rights_uri=RightsStatus.IN_COPYRIGHT,
        )
        format = FormatData(
            content_type=link.media_type,
            drm_scheme=DeliveryMechanism.ADOBE_DRM,
            link=link,
            rights_uri=RightsStatus.IN_COPYRIGHT,
        )

        circulation_data = CirculationData(
            data_source=DataSource.OVERDRIVE,
            primary_identifier=identifier,
            collection=self.collection,
            links=[link],
            formats=[format],
        )

        replace = ReplacementPolicy(
            formats=True,
        )

        pool, ignore = circulation_data.license_pool(self._db)
        circulation_data.apply(pool, replace)
        eq_(False, pool.open_access)
        eq_(1, len(pool.delivery_mechanisms))
        eq_(RightsStatus.IN_COPYRIGHT, pool.delivery_mechanisms[0].rights_status.uri)


class TestMetaToModelUtility(DatabaseTest):

    def test_open_access_content_mirrored(self):
        # Make sure that open access material links are translated to our S3 buckets, and that 
        # commercial material links are left as is.
        # Note: Mirroring tests passing does not guarantee that all code now 
        # correctly calls on CirculationData, as well as Metadata.  This is a risk.

        mirror = DummyS3Uploader()
        # Here's a book.
        edition, pool = self._edition(with_license_pool=True)

        # Here's a link to the content of the book, which will be mirrored.
        link_mirrored = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD, href="http://example.com/",
            media_type=Representation.EPUB_MEDIA_TYPE,
            content="i am a tiny book"
        )

        # This link will not be mirrored.
        link_unmirrored = LinkData(
            rel=Hyperlink.DRM_ENCRYPTED_DOWNLOAD, href="http://example.com/2",
            media_type=Representation.EPUB_MEDIA_TYPE,
            content="i am a pricy book"
        )

        # Apply the metadata.
        policy = ReplacementPolicy(mirror=mirror)

        metadata = Metadata(data_source=edition.data_source, 
        	links=[link_mirrored, link_unmirrored],
    	)
        metadata.apply(edition, replace=policy)
        # make sure the refactor is done right, and metadata does not upload
        eq_(0, len(mirror.uploaded))


        circulation_data = CirculationData(
            data_source=edition.data_source, 
            primary_identifier=edition.primary_identifier,
            collection=self._collection(),
            links=[link_mirrored, link_unmirrored],
        )
        circulation_data.apply(pool, replace=policy)
        
        # make sure the refactor is done right, and circulation does upload 
        eq_(1, len(mirror.uploaded))

        # Only the open-access link has been 'mirrored'.
        [book] = mirror.uploaded

        # It's remained an open-access link.
        eq_(
            [Hyperlink.OPEN_ACCESS_DOWNLOAD], 
            [x.rel for x in book.resource.links]
        )


        # It's been 'mirrored' to the appropriate S3 bucket.
        assert book.mirror_url.startswith('http://s3.amazonaws.com/test.content.bucket/')
        expect = '/%s/%s.epub' % (
            edition.primary_identifier.identifier,
            edition.title
        )
        assert book.mirror_url.endswith(expect)

        # make sure the mirrored link is safely on edition
        sorted_edition_links = sorted(edition.license_pool.identifier.links, key=lambda x: x.rel)
        unmirrored_representation, mirrored_representation = [edlink.resource.representation for edlink in sorted_edition_links]
        assert mirrored_representation.mirror_url.startswith('http://s3.amazonaws.com/test.content.bucket/')

        # make sure the unmirrored link is safely on edition
        eq_('http://example.com/2', unmirrored_representation.url)
        # make sure the unmirrored link has not been translated to an S3 URL
        eq_(None, unmirrored_representation.mirror_url)


    def test_mirror_open_access_link_fetch_failure(self):
        mirror = DummyS3Uploader()
        h = DummyHTTPClient()

        edition, pool = self._edition(with_license_pool=True)

        data_source = DataSource.lookup(self._db, DataSource.GUTENBERG)
        policy = ReplacementPolicy(mirror=mirror, http_get=h.do_get)
        circulation_data = CirculationData(
            data_source=edition.data_source, 
            primary_identifier=edition.primary_identifier,
            collection=self._collection(),
        )

        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url,
        )

        link_obj, ignore = edition.primary_identifier.add_link(
            rel=link.rel, href=link.href, data_source=data_source,
            license_pool=pool, media_type=link.media_type,
            content=link.content,
        )

        h.queue_response(403)
        
        circulation_data.mirror_link(pool, data_source, link, link_obj, policy)

        representation = link_obj.resource.representation

        # Fetch failed, so we should have a fetch exception but no mirror url.
        assert representation.fetch_exception != None
        eq_(None, representation.mirror_exception)
        eq_(None, representation.mirror_url)
        eq_(link.href, representation.url)
        assert representation.fetched_at != None
        eq_(None, representation.mirrored_at)

        # The license pool is suppressed when fetch fails.
        eq_(True, pool.suppressed)
        assert representation.fetch_exception in pool.license_exception


    def test_mirror_open_access_link_mirror_failure(self):
        mirror = DummyS3Uploader(fail=True)
        h = DummyHTTPClient()

        edition, pool = self._edition(with_license_pool=True)

        data_source = DataSource.lookup(self._db, DataSource.GUTENBERG)
        policy = ReplacementPolicy(mirror=mirror, http_get=h.do_get)

        circulation_data = CirculationData(
            data_source=edition.data_source, 
            primary_identifier=edition.primary_identifier,
            collection=self._collection(),
        )

        link = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            media_type=Representation.EPUB_MEDIA_TYPE,
            href=self._url,
        )

        link_obj, ignore = edition.primary_identifier.add_link(
            rel=link.rel, href=link.href, data_source=data_source,
            license_pool=pool, media_type=link.media_type,
            content=link.content,
        )

        h.queue_response(200, media_type=Representation.EPUB_MEDIA_TYPE)
        
        circulation_data.mirror_link(pool, data_source, link, link_obj, policy)

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
        assert representation.mirror_url.endswith("%s.epub" % edition.title)

        # Book content is still there since it wasn't mirrored.
        assert representation.content != None

        # The license pool is suppressed when mirroring fails.
        eq_(True, pool.suppressed)
        assert representation.mirror_exception in pool.license_exception

    def test_has_open_access_link(self):
        identifier = IdentifierData(Identifier.GUTENBERG_ID, "1")
        
        circulationdata = CirculationData(
            DataSource.GUTENBERG,
            identifier,
            collection=self._collection(),
        )

        # No links
        eq_(False, circulationdata.has_open_access_link)

        linkdata = LinkData(
            rel=Hyperlink.OPEN_ACCESS_DOWNLOAD,
            href=self._url,
        )
        circulationdata.links = [linkdata]
        
        # Open-access link with no explicit rights URI.
        eq_(True, circulationdata.has_open_access_link)

        # Open-access link with contradictory rights URI.
        linkdata.rights_uri = RightsStatus.IN_COPYRIGHT
        eq_(False, circulationdata.has_open_access_link)

        # Open-access link with consistent rights URI.
        linkdata.rights_uri = RightsStatus.GENERIC_OPEN_ACCESS
        eq_(True, circulationdata.has_open_access_link)
