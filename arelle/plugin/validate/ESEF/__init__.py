'''
Created on June 6, 2018

Filer Guidelines: 
  RTS: https://eur-lex.europa.eu/legal-content/EN/TXT/?qid=1563538104990&uri=CELEX:32019R0815
  ESEF Filer Manual https://www.esma.europa.eu/sites/default/files/library/esma32-60-254_esef_reporting_manual.pdf
  
Taxonomy Architecture: 

Taxonomy package expected to be installed: 

@author: Mark V Systems Limited
(c) Copyright 2018 Mark V Systems Limited, All rights reserved.

Using arelle as a web server:

   arelleCmdLine.exe --webserver localhost:8080:cheroot --plugins validate/ESEF --packages {my-package-directory}/esef_taxonomy_2019.zip
   
Client with curl:

   curl -X POST "-HContent-type: application/zip" -T TC1_valid.zip "http://localhost:8080/rest/xbrl/validation?disclosureSystem=esef&media=text"

'''
import os, base64
try:
    import regex as re
except ImportError:
    import re
from collections import defaultdict
from math import isnan
from lxml.etree import _ElementTree, _Comment, _ProcessingInstruction
from arelle import LeiUtil, ModelDocument, XbrlConst, XhtmlValidate, XmlUtil
from arelle.FunctionIxt import ixtNamespaces
from arelle.ModelDtsObject import ModelResource
from arelle.ModelInstanceObject import ModelFact, ModelInlineFact, ModelInlineFootnote
from arelle.ModelObject import ModelObject
from arelle.ModelValue import qname
from arelle.PackageManager import validateTaxonomyPackage
from arelle.PythonUtil import strTruncate
from arelle.UrlUtil import isHttpUrl, scheme
from arelle.XmlValidate import VALID, lexicalPatterns

from arelle.ValidateXbrlCalcs import inferredDecimals, rangeValue
from arelle.XbrlConst import (ixbrl11, xhtml, link, parentChild, summationItem, standardLabel,
                              all as hc_all, notAll as hc_notAll, hypercubeDimension, dimensionDomain, domainMember,
                              qnLinkLoc, qnLinkFootnoteArc, qnLinkFootnote, qnIXbrl11Footnote, iso17442)
from arelle.XmlValidate import VALID
from arelle.ValidateUtr import ValidateUtr
from .Const import (browserMaxBase64ImageLength, mandatory, untransformableTypes, outdatedTaxonomyURLs, esefTaxonomyURLs, esefFormulaMessagesURLs,
                    esefPrimaryStatementPlaceholderNames, esefStatementsOfMonetaryDeclarationNames, esefMandatoryElementNames2020)
from .Dimensions import checkFilingDimensions
from .DTS import checkFilingDTS
from .Util import isExtension, checkImageContents

styleIxHiddenPattern = re.compile(r"(.*[^\w]|^)-esef-ix-hidden\s*:\s*([\w.-]+).*")
ifrsNsPattern = re.compile(r"http://xbrl.ifrs.org/taxonomy/[0-9-]{10}/ifrs-full")
datetimePattern = lexicalPatterns["XBRLI_DATEUNION"]
imgDataMediaBase64Pattern = re.compile(r"data:image([^,]*);base64,")
ixErrorPattern = re.compile(r"ix11[.]|xmlSchema[:]|(?!xbrl.5.2.5.2|xbrl.5.2.6.2)xbrl[.]|xbrld[ti]e[:]|utre[:]")
docTypeXhtmlPattern = re.compile(r"^<!(?:DOCTYPE\s+)\s*html(?:PUBLIC\s+)?(?:.*-//W3C//DTD\s+(X?HTML)\s)?.*>$", re.IGNORECASE)

FOOTNOTE_LINK_CHILDREN = {qnLinkLoc, qnLinkFootnoteArc, qnLinkFootnote, qnIXbrl11Footnote}
PERCENT_TYPE = qname("{http://www.xbrl.org/dtr/type/numeric}num:percentItemType")
IXT_NAMESPACES = {ixtNamespaces["ixt v4"]} # only tr4 is currently recommended

def etreeIterWithDepth(node, depth=0):
    yield (node, depth)
    for child in node.iterchildren():
        for n_d in etreeIterWithDepth(child, depth+1):
            yield n_d
                
def dislosureSystemTypes(disclosureSystem, *args, **kwargs):
    # return ((disclosure system name, variable name), ...)
    return (("ESEF", "ESEFplugin"),)

def disclosureSystemConfigURL(disclosureSystem, *args, **kwargs):
    return os.path.join(os.path.dirname(__file__), "config.xml")

def modelXbrlBeforeLoading(modelXbrl, normalizedUri, filepath, isEntry=False, **kwargs):
    if getattr(modelXbrl.modelManager.disclosureSystem, "ESEFplugin", False):
        if isEntry and not any("unconsolidated" in n for n in modelXbrl.modelManager.disclosureSystem.names):
            if modelXbrl.fileSource.isArchive and not validateTaxonomyPackage(modelXbrl.modelManager.cntlr, modelXbrl.fileSource):
                modelXbrl.error("ESEF.RTS.Annex.III.3.missingOrInvalidTaxonomyPackage",
                    _("Single reporting package with issuer's XBRL extension taxonomy files and Inline XBRL instance document must be compliant with the latest recommended version of the Taxonomy Packages specification (1.0)"),
                    modelObject=modelXbrl) 
                return ModelDocument.LoadingException("Invalid taxonomy package") 
    return None                       

def modelXbrlLoadComplete(modelXbrl):
    if getattr(modelXbrl.modelManager.disclosureSystem, "ESEFplugin", False):
        if any("unconsolidated" in n for n in modelXbrl.modelManager.disclosureSystem.names):
            htmlElement = modelXbrl.modelDocument.xmlRootElement
            if htmlElement.namespaceURI == xhtml:  
                # xhtml is validated by ModelDocument inine, plain xhtml still needs validating
                XhtmlValidate.xhtmlValidate(modelXbrl, htmlElement)
            if modelXbrl.facts:
                modelXbrl.warning("arelle-ESEF:unconsolidatedContainsInline",
                                _("Inline XBRL not expected in unconsolidated xhtml document."), 
                                modelObject=modelXbrl)
            return
        if modelXbrl.modelDocument is None:
            modelXbrl.error("ESEF.3.1.3.missingOrInvalidTaxonomyPackage",
                            _("RTS Annex III Par 3 and ESEF 3.1.3 requires an XBRL Report Package but one could not be loaded."), 
                            modelObject=modelXbrl)
        if (modelXbrl.modelDocument is None or 
            (modelXbrl.modelDocument.type not in (ModelDocument.Type.TESTCASESINDEX, ModelDocument.Type.TESTCASE, ModelDocument.Type.REGISTRY, ModelDocument.Type.RSSFEED)
            and not modelXbrl.facts and "ESEF.RTS.Art.6.a.noInlineXbrlTags" not in modelXbrl.errors)):
            modelXbrl.error("ESEF.RTS.Art.6.a.noInlineXbrlTags",
                            _("RTS on ESEF requires inline XBRL, no facts were reported."),
                            modelObject=modelXbrl)
        # add in formula messages if not loaded
        for docUrl in modelXbrl.urlDocs:
            if docUrl in esefFormulaMessagesURLs and esefFormulaMessagesURLs[docUrl] not in modelXbrl.urlDocs:
                priorValidateDisclosureSystem = modelXbrl.modelManager.validateDisclosureSystem
                modelXbrl.modelManager.validateDisclosureSystem = False
                ModelDocument.load(modelXbrl,esefFormulaMessagesURLs[docUrl],isSupplemental=True)
                modelXbrl.modelManager.validateDisclosureSystem = priorValidateDisclosureSystem
                modelXbrl.relationshipSets.clear() # relationships have to be re-cached
                break

def validateXbrlStart(val, parameters=None, *args, **kwargs):
    val.validateESEFplugin = val.validateDisclosureSystem and getattr(val.disclosureSystem, "ESEFplugin", False)
    if not (val.validateESEFplugin):
        return
    val.extensionImportedUrls = set()
    val.unconsolidated = any("unconsolidated" in n for n in val.disclosureSystem.names)
    val.consolidated = not val.unconsolidated
    
def validateXbrlFinally(val, *args, **kwargs):
    if not (val.validateESEFplugin):
        return

    _xhtmlNs = "{{{}}}".format(xhtml)
    _xhtmlNsLen = len(_xhtmlNs)
    modelXbrl = val.modelXbrl
    modelDocument = modelXbrl.modelDocument
    if not modelDocument:
        return # never loaded properly

    _statusMsg = _("validating {0} filing rules").format(val.disclosureSystem.name)
    modelXbrl.profileActivity()
    modelXbrl.modelManager.showStatus(_statusMsg)
    
    reportXmlLang = None
    firstRootmostXmlLangDepth = 9999999
    
    _ifrsNs = None
    for targetNs in modelXbrl.namespaceDocs.keys():
        if ifrsNsPattern.match(targetNs):
            _ifrsNs = targetNs
            break
    if val.consolidated and not _ifrsNs:
        modelXbrl.warning("ESEF.RTS.ifrsRequired",
                        _("RTS on ESEF requires IFRS taxonomy."), 
                        modelObject=modelXbrl)
        return
    
    esefPrimaryStatementPlaceholders = set(qname(_ifrsNs, n) for n in esefPrimaryStatementPlaceholderNames)
    esefStatementsOfMonetaryDeclaration = set(qname(_ifrsNs, n) for n in esefStatementsOfMonetaryDeclarationNames)
    esefMandatoryElements2020 = set(qname(_ifrsNs, n) for n in esefMandatoryElementNames2020)
    
    if modelDocument.type == ModelDocument.Type.INSTANCE and not val.unconsolidated:
        modelXbrl.error("ESEF.I.1.instanceShallBeInlineXBRL",
                        _("RTS on ESEF requires inline XBRL instances."), 
                        modelObject=modelXbrl)
        
    checkFilingDimensions(val) # sets up val.primaryItems and val.domainMembers
    val.hasExtensionSchema = val.hasExtensionPre = val.hasExtensionCal = val.hasExtensionDef = val.hasExtensionLbl = False
    checkFilingDTS(val, modelXbrl.modelDocument, [])
    modelXbrl.profileActivity("... filer DTS checks", minTimeToShow=1.0)
    
    if val.consolidated and not (val.hasExtensionSchema and val.hasExtensionPre and val.hasExtensionCal and val.hasExtensionDef and val.hasExtensionLbl):
        missingFiles = []
        if not val.hasExtensionSchema: missingFiles.append("schema file")
        if not val.hasExtensionPre: missingFiles.append("presentation linkbase")
        if not val.hasExtensionCal: missingFiles.append("calculation linkbase")
        if not val.hasExtensionDef: missingFiles.append("definition linkbase")
        if not val.hasExtensionLbl: missingFiles.append("label linkbase")
        modelXbrl.error("ESEF.3.1.1.extensionTaxonomyWrongFilesStructure",
            _("Extension taxonomies MUST consist of at least a schema file and presentation, calculation, definition and label linkbases"
              ": missing %(missingFiles)s"),
            modelObject=modelXbrl, missingFiles=", ".join(missingFiles))
        
    #if modelDocument.type == ModelDocument.Type.INLINEXBRLDOCUMENTSET:
    #    # reports only under reports, none elsewhere
    #    modelXbrl.fileSource.dir
        
    if modelDocument.type in (ModelDocument.Type.INLINEXBRL, ModelDocument.Type.INLINEXBRLDOCUMENTSET, ModelDocument.Type.INSTANCE, ModelDocument.Type.UnknownXML):
        footnotesRelationshipSet = modelXbrl.relationshipSet("XBRL-footnotes")
        orphanedFootnotes = set()
        noLangFootnotes = set()
        factLangFootnotes = defaultdict(set)
        footnoteRoleErrors = set()
        transformRegistryErrors = set()
        def checkFootnote(elt, text):
            if text: # non-empty footnote must be linked to a fact if not empty
                if not any(isinstance(rel.fromModelObject, ModelFact)
                           for rel in footnotesRelationshipSet.toModelObject(elt)):
                    orphanedFootnotes.add(elt)
            lang = elt.xmlLang
            if not lang:
                noLangFootnotes.add(elt)
            else:
                for rel in footnotesRelationshipSet.toModelObject(elt):
                    if rel.fromModelObject is not None:
                        factLangFootnotes[rel.fromModelObject].add(lang)
            if elt.role != XbrlConst.footnote or not all(
                rel.arcrole == XbrlConst.factFootnote and rel.linkrole == XbrlConst.defaultLinkRole
                for rel in footnotesRelationshipSet.toModelObject(elt)):
                footnoteRoleErrors.add(elt)
                
        # check file name of each inline document (which might be below a top-level IXDS)
        ixdsDocDirs = set()
        for doc in modelXbrl.urlDocs.values():
            if doc.type in (ModelDocument.Type.INLINEXBRL, ModelDocument.Type.UnknownXML):
                _baseName, _baseExt = os.path.splitext(doc.basename)
                if _baseExt not in (".xhtml",".html"):
                    modelXbrl.error("ESEF.2.6.1.incorrectFileExtension",
                        _("Inline XBRL document included within a ESEF report package MUST have a .html or .xhtml extension: %(fileName)s"),
                        modelObject=doc, fileName=doc.basename)
                docinfo = doc.xmlRootElement.getroottree().docinfo
                docTypeMatch = docTypeXhtmlPattern.match(docinfo.doctype)
                if val.consolidated:
                    if docTypeMatch:
                        if not docTypeMatch.group(1) or docTypeMatch.group(1).lower() == "html":
                            modelXbrl.error("ESEF.RTS.Art.3.htmlDoctype",
                                _("Doctype SHALL NOT specify html: %(doctype)s"),
                                modelObject=doc, doctype=docinfo.doctype)
                        else:
                            modelXbrl.warning("ESEF.RTS.Art.3.xhtmlDoctype",
                                _("Doctype implies xhtml DTD validation but inline 1.1 requires schema validation: %(doctype)s"),
                                modelObject=doc, doctype=docinfo.doctype)
                    if doc.ixNS != ixbrl11:
                        modelXbrl.error("ESEF.RTS.Annex.III.Par.1.invalidInlineXBRL",
                            _("Invalid inline XBRL namespace: %(namespace)s"),
                            modelObject=doc, namespace=doc.ixNS)
                    # check location in a taxonomy package
                    # ixds loading for ESEF expects all xhtml instances to be combined into single IXDS regardless of directory in report zip
                    docDirPath = re.split(r"[/\\]", doc.uri)
                    reportCorrectlyPlacedInPackage = reportIsInZipFile = False
                    for i, dir in enumerate(docDirPath):
                        if dir.lower().endswith(".zip"):
                            reportIsInZipFile = True
                            packageName = dir[:-4] # web service posted zips are always named POSTupload.zip instead of the source file name
                            if len(docDirPath) >= i + 2 and packageName in (docDirPath[i+1],"POSTupload") and docDirPath[i+2] == "reports":
                                ixdsDocDirs.add("/".join(docDirPath[i+3:-1]))
                                reportCorrectlyPlacedInPackage = True
                            else:
                                ixdsDocDirs.add("/".join(docDirPath[i+1:len(docDirPath)-1])) # needed for error msg on orphaned instance docs
                            break
                    if not reportIsInZipFile:
                        modelXbrl.error("ESEF.2.6.1.reportIncorrectlyPlacedInPackage",
                            _("Inline XBRL document MUST be included within an ESEF report package as defined in"
                               "http://www.xbrl.org/WGN/report-packages/WGN-2018-08-14/report-packages-WGN-2018-08-14"
                               ".html: %(fileName)s (Document is not in a zip archive)"),
                            modelObject=doc, fileName=doc.basename)
                    elif not reportCorrectlyPlacedInPackage:
                        modelXbrl.error("ESEF.2.6.1.reportIncorrectlyPlacedInPackage",
                             _("Inline XBRL document MUST be included within an ESEF report package as defined in"
                               "http://www.xbrl.org/WGN/report-packages/WGN-2018-08-14/report-packages-WGN-2018-08-14"
                               ".html: %(fileName)s (Document file not in correct place in package)"),
                            modelObject=doc, fileName=doc.basename)
                else: # non-consolidated
                    if docTypeMatch:
                        if not docTypeMatch.group(1) or docTypeMatch.group(1).lower() == "html":
                            modelXbrl.error("ESEF.RTS.Art.3.htmlDoctype",
                                _("Doctype SHALL NOT specify html validation: %(doctype)s"),
                                modelObject=doc, doctype=docinfo.doctype)
                        

        if len(ixdsDocDirs) > 1 and val.consolidated:
            modelXbrl.error("ESEF.2.6.2.reportSetIncorrectlyPlacedInPackage",
                     _("Multiple Inline XBRL documents MUST be included within a ESEF report package as defined in "
                       "http://www.xbrl.org/WGN/report-packages/WGN-2018-08-14/report-packages-WGN-2018-08-14.html: "
                       "%(documentSets)s (Document files appear to be in multiple document sets)"),
                modelObject=doc, documentSets=", ".join(sorted(ixdsDocDirs)))
        if modelDocument.type in (ModelDocument.Type.INLINEXBRL, ModelDocument.Type.INLINEXBRLDOCUMENTSET, ModelDocument.Type.UnknownXML):
            hiddenEltIds = {}
            presentedHiddenEltIds = defaultdict(list)
            eligibleForTransformHiddenFacts = []
            requiredToDisplayFacts = []
            requiredToDisplayFactIds = {}
            firstIxdsDoc = True
            for ixdsHtmlRootElt in (modelXbrl.ixdsHtmlElements if val.consolidated else # ix root elements for all ix docs in IXDS
                                    (modelXbrl.modelDocument.xmlRootElement,)): # plain xhtml filing
                ixNStag = getattr(ixdsHtmlRootElt.modelDocument, "ixNStag", ixbrl11)
                ixTags = set(ixNStag + ln for ln in ("nonNumeric", "nonFraction", "references", "relationship"))
                ixTextTags = set(ixNStag + ln for ln in ("nonFraction", "continuation", "footnote"))
                ixExcludeTag = ixNStag + "exclude"
                ixTupleTag = ixNStag + "tuple"
                ixFractionTag = ixNStag + "fraction"
                for elt, depth in etreeIterWithDepth(ixdsHtmlRootElt):
                    eltTag = elt.tag
                    if isinstance(elt, (_ElementTree, _Comment, _ProcessingInstruction)):
                        continue # comment or other non-parsed element
                    else:
                        eltTag = elt.tag
                        if eltTag.startswith(_xhtmlNs):
                            eltTag = eltTag[_xhtmlNsLen:]
                            if firstIxdsDoc and (not reportXmlLang or depth < firstRootmostXmlLangDepth):
                                xmlLang = elt.get("{http://www.w3.org/XML/1998/namespace}lang")
                                if xmlLang:
                                    reportXmlLang = xmlLang
                                    firstRootmostXmlLangDepth = depth
                        if ((eltTag in ("object", "script")) or
                            (eltTag == "a" and "javascript:" in elt.get("href","")) or
                            (eltTag == "img" and "javascript:" in elt.get("src",""))):
                            modelXbrl.error("ESEF.2.5.1.executableCodePresent",
                                _("Inline XBRL documents MUST NOT contain executable code: %(element)s"),
                                modelObject=elt, element=eltTag)
                        elif eltTag == "img":
                            src = elt.get("src","").strip()
                            hasParentIxTextTag = False # check if image is in an ix text-bearing element
                            _ancestorElt = elt
                            while (_ancestorElt is not None):
                                if _ancestorElt.tag == ixExcludeTag: # excluded from any parent text-bearing ix element
                                    break
                                if _ancestorElt.tag in ixTextTags:
                                    hasParentIxTextTag = True
                                    break
                                _ancestorElt = _ancestorElt.getparent()                     
                            if scheme(src) in ("http", "https", "ftp"):
                                modelXbrl.error("ESEF.4.1.6.xHTMLDocumentContainsExternalReferences" if val.unconsolidated
                                                else "ESEF.3.5.1.inlineXbrlDocumentContainsExternalReferences",
                                    _("Inline XBRL instance documents MUST NOT contain any reference pointing to resources outside the reporting package: %(element)s"),
                                    modelObject=elt, element=eltTag,
                                    messageCodes=("ESEF.3.5.1.inlineXbrlDocumentContainsExternalReferences", "ESEF.4.1.6.xHTMLDocumentContainsExternalReferences"))
                            elif not src.startswith("data:image"):
                                if hasParentIxTextTag:
                                    modelXbrl.error("ESEF.2.5.1.imageInIXbrlElementNotEmbedded",
                                        _("Images appearing within an inline XBRL element MUST be embedded regardless of their size."),
                                        modelObject=elt)
                                else:
                                    # presume it to be an image file, check image contents
                                    try:
                                        base = elt.modelDocument.baseForElement(elt)
                                        normalizedUri = elt.modelXbrl.modelManager.cntlr.webCache.normalizeUrl(src, base)
                                        if not elt.modelXbrl.fileSource.isInArchive(normalizedUri):
                                            normalizedUri = elt.modelXbrl.modelManager.cntlr.webCache.getfilename(normalizedUri)
                                        imglen = 0
                                        with elt.modelXbrl.fileSource.file(normalizedUri,binary=True)[0] as fh:
                                            imgContents = fh.read()
                                            imglen += len(imgContents)
                                            checkImageContents(modelXbrl, elt, os.path.splitext(src)[1], True, imgContents)
                                            imgContents = None # deref, may be very large
                                        #if imglen < browserMaxBase64ImageLength:
                                        #    modelXbrl.error("ESEF.2.5.1.imageIncludedAndNotEmbeddedAsBase64EncodedString",
                                        #        _("Images MUST be included in the XHTML document as a base64 encoded string unless their size exceeds support of browsers (%(maxImageSize)s): %(file)s."),
                                        #        modelObject=elt, maxImageSize=browserMaxBase64ImageLength, file=os.path.basename(normalizedUri))
                                    except IOError as err:
                                        modelXbrl.error("ESEF.2.5.1.imageFileCannotBeLoaded",
                                            _("Image file which isn't openable '%(src)s', error: %(error)s"),
                                            modelObject=elt, src=src, error=err)
                            else:
                                m = imgDataMediaBase64Pattern.match(src)
                                if not m:
                                    modelXbrl.warning("ESEF.2.5.1.embeddedImageNotUsingBase64Encoding",
                                        _("Images included in the XHTML document SHOULD be base64 encoded: %(src)s."),
                                        modelObject=elt, src=src[:128])
                                    if "," in imgContents:
                                        imgContents = imgContents.partition(",")[2]
                                    checkImageContents(modelXbrl, elt, mime, False, imgContents)
                                else:
                                    if not m.group(1):
                                        modelXbrl.error("ESEF.2.5.1.MIMETypeNotSpecified",
                                            _("Images included in the XHTML document MUST be saved with MIME type specifying PNG, GIF, SVG or JPG/JPEG formats: %(src)s."),
                                            modelObject=elt, src=src[:128])
                                    elif m.group(1) not in ("/gif", "/jpeg", "/jpg", "/png", "/svg+xml"):
                                        modelXbrl.error("ESEF.2.5.1.imageFormatNotSupported",
                                            _("Images included in the XHTML document MUST be saved in PNG, GIF, SVG or JPG/JPEG formats: %(src)s."),
                                            modelObject=elt, src=src[:128])
                                    # check for malicious image contents
                                    mime, _sep, b64ImgContents = src.partition(";base64,")
                                    try:
                                        imgContents = base64.b64decode(b64ImgContents) # allow embedded newlines
                                        checkImageContents(modelXbrl, elt, mime, False, imgContents)
                                        imgContents = None # deref, may be very large
                                    except base64.binascii.Error as err:
                                        modelXbrl.error("ESEF.2.5.1.embeddedImageNotUsingBase64Encoding",
                                            _("Base64 encoding error %(err)s in image source: %(src)s."),
                                            modelObject=elt, err=str(err), src=src[:128])
                        # links to external documents are allowed as of 2021 per G.2.5.1 
                        #    Since ESEF is a format requirement and is not expected to impact the 'human readable layer' of a report, 
                        #    this guidance should not be seen as limiting the inclusion of links to external websites, to other documents 
                        #    or to other sections of the annual financial report.'''
                        #elif eltTag == "a":
                        #    href = elt.get("href","").strip()
                        #    if scheme(href) in ("http", "https", "ftp"):
                        #        modelXbrl.error("ESEF.4.1.6.xHTMLDocumentContainsExternalReferences" if val.unconsolidated
                        #                        else "ESEF.3.5.1.inlineXbrlDocumentContainsExternalReferences",
                        #            _("Inline XBRL instance documents MUST NOT contain any reference pointing to resources outside the reporting package: %(element)s"),
                        #            modelObject=elt, element=eltTag,
                        #            messageCodes=("ESEF.3.5.1.inlineXbrlDocumentContainsExternalReferences", "ESEF.4.1.6.xHTMLDocumentContainsExternalReferences"))
                        elif eltTag == "base":
                            modelXbrl.error("ESEF.2.4.2.htmlOrXmlBaseUsed",
                                _("The HTML <base> elements MUST NOT be used in the Inline XBRL document."),
                                modelObject=elt, element=eltTag)
                        elif eltTag == "link" and elt.get("type") == "text/css":
                            if val.unconsolidated:
                                modelXbrl.warning("ESEF.4.1.4.externalCssFileForXhtmlDocument",
                                    _("For XHTML stand-alone documents, the CSS SHOULD be embedded within the document."),
                                    modelObject=elt, element=eltTag)
                            elif len(modelXbrl.ixdsHtmlElements) > 1:
                                f = elt.get("href")
                                if not f or isHttpUrl(f) or os.path.isabs(f):
                                    modelXbrl.warning("ESEF.2.5.4.externalCssReportPackage",
                                        _("The CSS file should be physically stored within the report package: %{file}s."),
                                        modelObject=elt, file=f)
                            else:
                                modelXbrl.warning("ESEF.2.5.4.externalCssFileForSingleIXbrlDocument",
                                    _("Where an Inline XBRL document set contains a single document, the CSS SHOULD be embedded within the document."),
                                    modelObject=elt, element=eltTag)
                        elif val.unconsolidated:
                            pass # rest of following tests don't apply to unconsolidated
                        elif eltTag == "style" and elt.get("type") == "text/css":
                            if len(modelXbrl.ixdsHtmlElements) > 1:
                                modelXbrl.warning("ESEF.2.5.4.embeddedCssForMultiHtmlIXbrlDocumentSets",
                                    _("Where an Inline XBRL document set contains multiple documents, the CSS SHOULD be defined in a separate file."),
                                    modelObject=elt, element=eltTag)
                                
                            
                    if eltTag in ixTags and elt.get("target"):
                        modelXbrl.warning("ESEF.2.5.3.targetAttributeUsedForESEFContents",
                            _("Target attribute SHOULD not be used unless explicitly required by local jurisdictions: element %(localName)s, target attribute %(target)s."),
                            modelObject=elt, localName=elt.elementQname, target=elt.get("target"))
                    if eltTag == ixTupleTag:
                        modelXbrl.error("ESEF.2.4.1.tupleElementUsed",
                            _("The ix:tuple element MUST not be used in the Inline XBRL document: %(qname)s."),
                            modelObject=elt, qname=elt.qname)
                    if eltTag == ixFractionTag:
                        modelXbrl.error("ESEF.2.4.1.fractionElementUsed",
                            _("The ix:fraction element MUST not be used in the Inline XBRL document."),
                            modelObject=elt)
                    if elt.get("{http://www.w3.org/XML/1998/namespace}base") is not None:
                        modelXbrl.error("ESEF.2.4.2.htmlOrXmlBaseUsed",
                            _("xml:base attributes MUST NOT be used in the Inline XBRL document: element %(localName)s, base attribute %(base)s."),
                            modelObject=elt, localName=elt.elementQname, base=elt.get("{http://www.w3.org/XML/1998/namespace}base"))
                    if isinstance(elt, ModelInlineFootnote):
                        checkFootnote(elt, elt.value)
                    elif isinstance(elt, ModelResource) and elt.qname == XbrlConst.qnLinkFootnote:
                        checkFootnote(elt, elt.value)
                    elif isinstance(elt, ModelInlineFact):
                        if elt.format is not None and elt.format.namespaceURI not in IXT_NAMESPACES:
                            transformRegistryErrors.add(elt)
                for ixHiddenElt in ixdsHtmlRootElt.iterdescendants(tag=ixNStag + "hidden"):
                    for tag in (ixNStag + "nonNumeric", ixNStag+"nonFraction"):
                        for ixElt in ixHiddenElt.iterdescendants(tag=tag):
                            if (getattr(ixElt, "xValid", 0) >= VALID  # may not be validated
                                ): # add future "and" conditions on elements which can be in hidden
                                if (ixElt.concept.baseXsdType not in untransformableTypes and
                                    not ixElt.isNil):
                                    eligibleForTransformHiddenFacts.append(ixElt)
                                elif ixElt.id is None:
                                    requiredToDisplayFacts.append(ixElt)
                            if ixElt.id:
                                hiddenEltIds[ixElt.id] = ixElt
                firstIxdsDoc = False

            if val.unconsolidated:
                modelXbrl.modelManager.showStatus(None)
                return # no more checks apply
            if eligibleForTransformHiddenFacts:
                modelXbrl.error("ESEF.2.4.1.transformableElementIncludedInHiddenSection",
                    _("The ix:hidden section of Inline XBRL document MUST not include elements eligible for transformation. "
                      "%(countEligible)s fact(s) were eligible for transformation: %(elements)s"),
                    modelObject=eligibleForTransformHiddenFacts, 
                    countEligible=len(eligibleForTransformHiddenFacts),
                    elements=", ".join(sorted(set(str(f.qname) for f in eligibleForTransformHiddenFacts))))
            for ixdsHtmlRootElt in modelXbrl.ixdsHtmlElements:
                for ixElt in ixdsHtmlRootElt.getroottree().iterfind("//{http://www.w3.org/1999/xhtml}*[@style]"):
                    hiddenFactRefMatch = styleIxHiddenPattern.match(ixElt.get("style",""))
                    if hiddenFactRefMatch:
                        hiddenFactRef = hiddenFactRefMatch.group(2)
                        if hiddenFactRef not in hiddenEltIds:
                            modelXbrl.error("ESEF.2.4.1.esefIxHiddenStyleNotLinkingFactInHiddenSection",
                                _("\"-esef-ix-hidden\" style identifies @id, %(id)s of a fact that is not in ix:hidden section."),
                                modelObject=ixElt, id=hiddenFactRef)
                        else:
                            presentedHiddenEltIds[hiddenFactRef].append(ixElt)
            for hiddenEltId, ixElt in hiddenEltIds.items():
                if (hiddenEltId not in presentedHiddenEltIds and
                    getattr(ixElt, "xValid", 0) >= VALID and # may not be validated
                    (ixElt.concept.baseXsdType in untransformableTypes or ixElt.isNil)):
                    requiredToDisplayFacts.append(ixElt)
            if requiredToDisplayFacts:
                modelXbrl.error("ESEF.2.4.1.factInHiddenSectionNotInReport",
                    _("The ix:hidden section contains %(countUnreferenced)s fact(s) whose @id is not applied on any \"-esef-ix- hidden\" style: %(elements)s"),
                    modelObject=requiredToDisplayFacts, 
                    countUnreferenced=len(requiredToDisplayFacts),
                    elements=", ".join(sorted(set(str(f.qname) for f in requiredToDisplayFacts))))
            del eligibleForTransformHiddenFacts, hiddenEltIds, presentedHiddenEltIds, requiredToDisplayFacts
        elif modelDocument.type == ModelDocument.Type.INSTANCE:
            for elt in modelDocument.xmlRootElement.iter():
                if elt.qname == XbrlConst.qnLinkFootnote: # for now assume no private elements extend link:footnote
                    checkFootnote(elt, elt.stringValue)
                        
        if val.unconsolidated:
            modelXbrl.modelManager.showStatus(None)
            return # no more checks apply
               
        contextsWithDisallowedOCEs = []
        contextsWithDisallowedOCEcontent = []
        contextsWithPeriodTime = []
        contextsWithPeriodTimeZone = []
        contextIdentifiers = defaultdict(list)
        nonStandardTypedDimensions = defaultdict(set)
        for context in modelXbrl.contexts.values():
            for elt in context.iterdescendants("{http://www.xbrl.org/2003/instance}startDate",
                                               "{http://www.xbrl.org/2003/instance}endDate",
                                               "{http://www.xbrl.org/2003/instance}instant"):
                m = datetimePattern.match(elt.stringValue)
                if m:
                    if m.group(1):
                        contextsWithPeriodTime.append(context)
                    if m.group(3):
                        contextsWithPeriodTimeZone.append(context)
            for elt in context.iterdescendants("{http://www.xbrl.org/2003/instance}segment"):
                contextsWithDisallowedOCEs.append(context)
                break
            for elt in context.iterdescendants("{http://www.xbrl.org/2003/instance}scenario"):
                if isinstance(elt,ModelObject):
                    if any(True for child in elt.iterchildren()
                                if isinstance(child,ModelObject) and 
                                   child.tag not in ("{http://xbrl.org/2006/xbrldi}explicitMember",
                                                     "{http://xbrl.org/2006/xbrldi}typedMember")):
                        contextsWithDisallowedOCEcontent.append(context)
            # check periods here
            contextIdentifiers[context.entityIdentifier].append(context)
                
        if contextsWithDisallowedOCEs:
            modelXbrl.error("ESEF.2.1.3.segmentUsed",
                _("xbrli:segment container MUST NOT be used in contexts: %(contextIds)s"),
                modelObject=contextsWithDisallowedOCEs, contextIds=", ".join(c.id for c in contextsWithDisallowedOCEs))
        if contextsWithDisallowedOCEcontent:
            modelXbrl.error("ESEF.2.1.3.scenarioContainsNonDimensionalContent",
                _("xbrli:scenario in contexts MUST NOT contain any other content than defined in XBRL Dimensions specification: %(contextIds)s"),
                modelObject=contextsWithDisallowedOCEcontent, contextIds=", ".join(c.id for c in contextsWithDisallowedOCEcontent))
        if len(contextIdentifiers) > 1:
            modelXbrl.error("ESEF.2.1.4.multipleIdentifiers",
                _("All entity identifiers in contexts MUST have identical content: %(contextIds)s"),
                modelObject=modelXbrl, contextIds=", ".join(i[1] for i in contextIdentifiers))
        for (contextScheme, contextIdentifier), contextElts in contextIdentifiers.items():
            if contextScheme != iso17442:
                modelXbrl.warning("ESEF.2.1.1.nonLEIContextScheme",
                    _("The scheme attribute of the xbrli:identifier element should have \"%(leiScheme)s\" as its content: %(contextScheme)s"),
                    modelObject=contextElts, contextScheme=contextScheme, leiScheme=iso17442)
            else:
                leiValidity = LeiUtil.checkLei(contextIdentifier)
                if leiValidity == LeiUtil.LEI_INVALID_LEXICAL:
                    modelXbrl.error("ESEF.2.1.1.invalidIdentifierFormat",
                        _("The LEI context identifier has an invalid format: %(identifier)s"),
                        modelObject=contextElts, identifier=contextIdentifier)
                elif leiValidity == LeiUtil.LEI_INVALID_CHECKSUM:
                    modelXbrl.error("ESEF.2.1.1.invalidIdentifier",
                        _("The LEI context identifier has checksum error: %(identifier)s"),
                        modelObject=contextElts, identifier=contextIdentifier)
        if contextsWithPeriodTime:
            modelXbrl.error("ESEF.2.1.2.periodWithTimeContent",
                _("The xbrli:startDate, xbrli:endDate and xbrli:instant elements MUST identify periods using whole days (i.e. specified without a time content): %(contextIds)s"),
                modelObject=contextsWithPeriodTime, contextIds=", ".join(c.id for c in contextsWithPeriodTime))
        if contextsWithPeriodTimeZone:
            modelXbrl.error("ESEF.2.1.2.periodWithTimeZone",
                _("The xbrli:startDate, xbrli:endDate and xbrli:instant elements MUST identify periods using whole days (i.e. specified without a time zone): %(contextIds)s"),
                modelObject=contextsWithPeriodTimeZone, contextIds=", ".join(c.id for c in contextsWithPeriodTimeZone))
        
        # identify unique contexts and units
        mapContext = {}
        mapUnit = {}
        uniqueContextHashes = {}
        for context in modelXbrl.contexts.values():
            h = context.contextDimAwareHash
            if h in uniqueContextHashes:
                if context.isEqualTo(uniqueContextHashes[h]):
                    mapContext[context] = uniqueContextHashes[h]
            else:
                uniqueContextHashes[h] = context
        del uniqueContextHashes
        uniqueUnitHashes = {}
        utrValidator = ValidateUtr(modelXbrl)
        utrUnitIds = set(u.unitId
                         for unitItemType in utrValidator.utrItemTypeEntries.values()
                         for u in unitItemType.values())
        for unit in modelXbrl.units.values():
            h = unit.hash
            if h in uniqueUnitHashes:
                if unit.isEqualTo(uniqueUnitHashes[h]):
                    mapUnit[unit] = uniqueUnitHashes[h]
            else:
                uniqueUnitHashes[h] = unit
            # check if any custom measure is in UTR
            for measureTerm in unit.measures:
                for measure in measureTerm:
                    ns = measure.namespaceURI
                    if ns != XbrlConst.iso4217 and not ns.startswith("http://www.xbrl.org/"):
                        if measure.localName in utrUnitIds:
                            modelXbrl.warning("ESEF.RTS.III.1.G1-7-1.customUnitInUtr",
                                _("Custom measure SHOULD NOT duplicate a UnitID of UTR: %(measure)s"),
                                modelObject=unit, measure=measure)
        del uniqueUnitHashes
        
        reportedMandatory = set()
        precisionFacts = set()
        numFactsByConceptContextUnit = defaultdict(list)
        textFactsByConceptContext = defaultdict(list)
        footnotesRelationshipSet = modelXbrl.relationshipSet(XbrlConst.factFootnote, XbrlConst.defaultLinkRole)
        noLangFacts = []
        textFactsMissingReportLang = []
        conceptsUsed = set()
        langsUsedByTextFacts = set()
                
        hasNoFacts = True
        for qn, facts in modelXbrl.factsByQname.items():
            hasNoFacts = False
            if qn in mandatory:
                reportedMandatory.add(qn)
            for f in facts:
                if f.precision is not None:
                    precisionFacts.add(f)
                if f.isNumeric and f.concept is not None and getattr(f, "xValid", 0) >= VALID:
                    numFactsByConceptContextUnit[(f.qname, mapContext.get(f.context,f.context), mapUnit.get(f.unit, f.unit))].append(f)
                    if not f.isNil and f.xValue > 1 and f.concept.type is not None and (
                        f.concept.type.qname == PERCENT_TYPE or f.concept.type.isDerivedFrom(PERCENT_TYPE)):
                        modelXbrl.warning("ESEF.2.2.2.percentGreaterThan100",
                            _("A percent fact should have value <= 100: %(element)s in context %(context)s value %(value)s"),
                            modelObject=f, element=f.qname, context=f.context.id, value=f.xValue)
                elif f.concept is not None and f.concept.type is not None:
                    if f.concept.type.isOimTextFactType:
                        lang = f.xmlLang
                        if not lang:
                            noLangFacts.append(f)
                        else:
                            langsUsedByTextFacts.add(lang)
                            if f.context is not None:
                                textFactsByConceptContext[(f.qname, mapContext.get(f.context,f.context))].append(f)
                conceptsUsed.add(f.concept)
                ''' only check line item concepts in 2020
                if f.context is not None:
                    for dim in f.context.qnameDims.values():
                        conceptsUsed.add(dim.dimension)
                        if dim.isExplicit:
                            conceptsUsed.add(dim.member)
                        #don't consider typed member as a used concept which needs to be in pre LB 
                        #elif dim.isTyped:
                        #    conceptsUsed.add(dim.typedMember)
                '''
            
        if noLangFacts:
            modelXbrl.error("ESEF.2.5.2.undefinedLanguageForTextFact",
                _("Each tagged text fact MUST have the 'xml:lang' attribute assigned or inherited."),
                modelObject=noLangFacts)
            
        # missing report lang text facts
        if reportXmlLang:
            for fList in textFactsByConceptContext.values():
                if not any(f.xmlLang == reportXmlLang for f in fList):
                    modelXbrl.error("ESEF.2.5.2.taggedTextFactOnlyInLanguagesOtherThanLanguageOfAReport",
                        _("Each tagged text fact MUST have the 'xml:lang' provided in at least the language of the report: %(element)s"),
                        modelObject=fList, element=fList[0].qname)
        
            
        # 2.2.4 test
        decVals = {}
        for fList in numFactsByConceptContextUnit.values():
            if len(fList) > 1:
                f0 = fList[0]
                if any(f.isNil for f in fList):
                    _inConsistent = not all(f.isNil for f in fList)
                else: # not all have same decimals
                    _d = inferredDecimals(f0)
                    _v = f0.xValue
                    _inConsistent = isnan(_v) # NaN is incomparable, always makes dups inconsistent
                    decVals[_d] = _v
                    aMax, bMin = rangeValue(_v, _d)
                    for f in fList[1:]:
                        _d = inferredDecimals(f)
                        _v = f.xValue
                        if isnan(_v):
                            _inConsistent = True
                            break
                        if _d in decVals:
                            _inConsistent |= _v != decVals[_d]
                        else:
                            decVals[_d] = _v
                        a, b = rangeValue(_v, _d)
                        if a > aMax: aMax = a
                        if b < bMin: bMin = b
                    if not _inConsistent:
                        _inConsistent = (bMin < aMax)
                    decVals.clear()
                if _inConsistent:
                    modelXbrl.error(("ESEF.2.2.4.inconsistentDuplicateNumericFactInInlineXbrlDocument"),
                        "Inconsistent duplicate numeric facts MUST NOT appear in the content of an inline XBRL document. %(fact)s that was used more than once in contexts equivalent to %(contextID)s: values %(values)s.  ",
                        modelObject=fList, fact=f0.qname, contextID=f0.contextID, values=", ".join(strTruncate(f.value, 128) for f in fList))

        if precisionFacts:
            modelXbrl.error("ESEF.2.2.1.precisionAttributeUsed",
                            _("The accuracy of numeric facts MUST be defined with the 'decimals' attribute rather than the 'precision' attribute: %(elements)s."), 
                            modelObject=precisionFacts, elements=", ".join(sorted(str(e.qname) for e in precisionFacts)))
            
        missingElements = (mandatory - reportedMandatory) 
        if missingElements:
            modelXbrl.error("ESEF.???.missingRequiredElements",
                            _("Required elements missing from document: %(elements)s."), 
                            modelObject=modelXbrl, elements=", ".join(sorted(str(qn) for qn in missingElements)))
            
        if transformRegistryErrors:
            modelXbrl.error("ESEF.2.2.3.incorrectTransformationRuleApplied",
                              _("ESMA recommends applying the Transformation Rules Registry 4, as published by XBRL International or any more recent versions of the Transformation Rules Registry provided with a 'Recommendation' status, for these elements: %(elements)s."),
                              modelObject=transformRegistryErrors, 
                              elements=", ".join(sorted(str(fact.qname) for fact in transformRegistryErrors)))
            
        if orphanedFootnotes:
            modelXbrl.warning("ESEF.2.3.1.unusedFootnote",
                _("Every nonempty link:footnote element SHOULD be linked to at least one fact."),
                modelObject=orphanedFootnotes)

        # this test removed from Filer Manual July 2020
        #if noLangFootnotes:
        #    modelXbrl.error("ESEF.2.3.1.undefinedLanguageForFootnote",
        #        _("Each footnote MUST have the 'xml:lang' attribute whose value corresponds to the language of the text in the content of the respective footnote."),
        #        modelObject=noLangFootnotes)
        ftLangNotUsedByTextFacts = set()
        ftLangNotUsedByTextLangs = set()
        for f,langs in factLangFootnotes.items():
            langsNotUsedByTextFacts = langs - langsUsedByTextFacts
            if langsNotUsedByTextFacts:
                ftLangNotUsedByTextFacts.add(f)
                ftLangNotUsedByTextLangs.update(langsNotUsedByTextFacts)
        if ftLangNotUsedByTextFacts:
            modelXbrl.error("ESEF.2.3.1.footnoteInLanguagesOtherThanLanguageOfContentOfAnyTextualFact",
                _("Each footnote MUST have or inherit an 'xml:lang' attribute whose value corresponds to the language of content of at least one textual fact present in the inline XBRL document, langs: %(langs)s; facts: %(qnames)s."),
                modelObject=ftLangNotUsedByTextFacts, qnames=", ".join(sorted(str(f.qname) for f in ftLangNotUsedByTextFacts)), langs=", ".join(sorted(ftLangNotUsedByTextLangs)))
        nonDefLangFtFacts = set(f for f,langs in factLangFootnotes.items() if reportXmlLang not in langs)
        if nonDefLangFtFacts:
            modelXbrl.error("ESEF.2.3.1.footnoteOnlyInLanguagesOtherThanLanguageOfAReport",
                _("Each fact MUST have at least one footnote with 'xml:lang' attribute whose value corresponds to the language of the text in the content of the respective footnote: %(qnames)s."),
                modelObject=nonDefLangFtFacts, qnames=", ".join(sorted(str(f.qname) for f in nonDefLangFtFacts)))
        del nonDefLangFtFacts
        if footnoteRoleErrors:
            modelXbrl.error("ESEF.2.3.1.nonStandardRoleForFootnote",
                _("The xlink:role attribute of a link:footnote and link:footnoteLink element as well as xlink:arcrole attribute of a link:footnoteArc MUST be defined in the XBRL Specification 2.1."),
                modelObject=footnoteRoleErrors)
            
        nonStdFootnoteElts = list()
        for modelLink in modelXbrl.baseSets[("XBRL-footnotes",None,None,None)]:
            for elt in modelLink.iterchildren():
                if isinstance(elt, (_ElementTree, _Comment, _ProcessingInstruction)):
                    continue # comment or other non-parsed element
                if elt.qname not in FOOTNOTE_LINK_CHILDREN:
                    nonStdFootnoteElts.append(elt)

        if nonStdFootnoteElts:
            modelXbrl.error("ESEF.2.3.2.nonStandardElementInFootnote",
                _("A link:footnoteLink element MUST have no children other than link:loc, link:footnote, and link:footnoteArc."),
                modelObject=nonStdFootnoteElts)
        
        conceptsUsedByFacts = conceptsUsed.copy()
        #for qn in modelXbrl.qnameDimensionDefaults.values():
        #    conceptsUsed.add(modelXbrl.qnameConcepts.get(qn))
            
        # 3.1.1 test
        hasOutdatedUrl = False
        for e in outdatedTaxonomyURLs:
            if e in val.extensionImportedUrls:
                val.modelXbrl.error("ESEF.3.1.2.incorrectEsefTaxonomyVersionUsed",
                     _("The issuer's extension taxonomies MUST import the applicable version of the taxonomy files prepared by ESMA. Outdated entry point: %(url)s"),
                    modelObject=modelDocument, url=e)
                hasOutdatedUrl = True
                
        if not hasOutdatedUrl and not any(e in val.extensionImportedUrls for e in esefTaxonomyURLs):
            val.modelXbrl.error("ESEF.3.1.2.requiredEntryPointNotImported",
                 _("The issuer's extension taxonomies MUST import the entry point of the taxonomy files prepared by ESMA."),
                modelObject=modelDocument)

            
        # unused elements in linkbases
        unreportedLbElts = set()
        for arcroles, err, checkRoots, lbType in (
                    ((parentChild,), "elements{}UsedForTagging{}AppliedInPresentationLinkbase", True, "presentation"),
                    ((summationItem,), "elements{}UsedForTagging{}AppliedInCalculationLinkbase", False, "calculation"),
                    ((hc_all, hc_notAll, dimensionDomain,domainMember), "elements{}UsedForTagging{}AppliedInDefinitionLinkbase", False, "definition")):
            if lbType == "calculation":
                reportedEltsNotInLb = set(c for c in conceptsUsedByFacts if c.isNumeric)
            else:
                reportedEltsNotInLb = conceptsUsedByFacts.copy()
            for arcrole in arcroles:
                for rel in modelXbrl.relationshipSet(arcrole).modelRelationships:
                    fr = rel.fromModelObject
                    to = rel.toModelObject
                    if arcrole in (parentChild, summationItem):
                        if fr is not None and not fr.isAbstract and fr not in conceptsUsed and isExtension(val, rel):
                            unreportedLbElts.add(fr)
                        if to is not None and not to.isAbstract and to not in conceptsUsed and isExtension(val, rel):
                            unreportedLbElts.add(to)
                    elif arcrole in (hc_all, domainMember, dimensionDomain):
                        # all primary items
                        if fr is not None and not fr.isAbstract and rel.isUsable and fr not in conceptsUsed and isExtension(val, rel) and not fr.type.isDomainItemType:
                            unreportedLbElts.add(to)
                        if to is not None and not to.isAbstract and rel.isUsable and to not in conceptsUsed and isExtension(val, rel) and not to.type.isDomainItemType:
                            unreportedLbElts.add(to)
                    reportedEltsNotInLb.discard(fr)
                    reportedEltsNotInLb.discard(to)
                    
            if reportedEltsNotInLb and lbType != "calculation":
                modelXbrl.error("ESEF.3.4.6.UsableConceptsNotAppliedByTaggedFacts",
                    _("All concepts used by tagged facts MUST be in extension taxonomy %(linkbaseType)s relationships: %(elements)s."),
                    modelObject=reportedEltsNotInLb, elements=", ".join(sorted((str(c.qname) for c in reportedEltsNotInLb))), linkbaseType=lbType)
        if unreportedLbElts:
            modelXbrl.error("ESEF.3.4.6.UsableConceptsNotAppliedByTaggedFacts",
                _("All usable concepts in extension taxonomy relationships MUST be applied by tagged facts: %(elements)s."),
                modelObject=unreportedLbElts, elements=", ".join(sorted((str(c.qname) for c in unreportedLbElts))))
                
        # 3.4.4 check for presentation preferred labels
        missingConceptLabels = defaultdict(set) # by role
        pfsConceptsRootInPreLB = set()
        # Annex II para 1 check of monetary declaration
        statementMonetaryUnitReportedConcepts = defaultdict(set) # index is unit, set is concepts
        statementMonetaryUnitFactCounts = {}
        
        def checkLabels(parent, relSet, labelrole, visited):
            if not parent.label(labelrole,lang=reportXmlLang,fallbackToQname=False):
                if parent.name != "NotesAccountingPoliciesAndMandatoryTags": # TEMPORARY TBD remove
                    missingConceptLabels[labelrole].add(parent)
            visited.add(parent)
            conceptRels = defaultdict(list) # counts for concepts without preferred label role
            for rel in relSet.fromModelObject(parent):
                child = rel.toModelObject
                if child is not None:
                    labelrole = rel.preferredLabel
                    if not labelrole:
                        conceptRels[child].append(rel)
                    if child not in visited:
                        checkLabels(child, relSet, labelrole, visited)
            for concept, rels in conceptRels.items():
                if len(rels) > 1:
                    modelXbrl.warning("ESEF.3.4.4.missingPreferredLabelRole",
                        _("Preferred label role SHOULD be used when concept is duplicated in same presentation tree location: %(qname)s."),
                        modelObject=rels+[concept], qname=concept.qname)
            visited.remove(parent)
            
        def checkMonetaryUnits(parent, relSet, visited):
            if parent.isMonetary:
                for f in modelXbrl.factsByQname.get(parent.qname,()):
                    u = f.unit
                    if u is not None and u.isSingleMeasure:
                        currency = u.measures[0][0].localName
                        statementMonetaryUnitReportedConcepts[currency].add(parent)
                        statementMonetaryUnitFactCounts[currency] = statementMonetaryUnitFactCounts.get(currency,0) + 1
            visited.add(parent)
            for rel in relSet.fromModelObject(parent):
                child = rel.toModelObject
                if child is not None:
                    if child not in visited:
                        checkMonetaryUnits(child, relSet, visited)
            visited.remove(parent)

        for ELR in modelXbrl.relationshipSet(parentChild).linkRoleUris:
            relSet = modelXbrl.relationshipSet(parentChild, ELR)
            pfsConceptsRootInELR = set()
            nonPfsConceptsRootInELR = set()

            for rootConcept in relSet.rootConcepts:
                checkLabels(rootConcept, relSet, None, set())
                # check for PFS element which isn't an orphan
                if relSet.fromModelObject(rootConcept):
                    if rootConcept.qname in esefPrimaryStatementPlaceholders:
                        pfsConceptsRootInPreLB.add(rootConcept)
                        pfsConceptsRootInELR.add(rootConcept)
                    else:
                        nonPfsConceptsRootInELR.add(rootConcept)
                # check for statement declaration of monetary concepts
                if rootConcept.qname in esefPrimaryStatementPlaceholders:
                    checkMonetaryUnits(rootConcept, relSet, set())
            if pfsConceptsRootInELR and (len(pfsConceptsRootInELR) + len(nonPfsConceptsRootInELR) ) > 1:
                roots = pfsConceptsRootInELR | nonPfsConceptsRootInELR
                modelXbrl.warning("ESEF.3.4.7.singleExtendedLinkRoleUsedForAllPFSs",
                    _("Separate Extended Link Roles are required by %(elr)s for hierarchies: %(roots)s."),
                    modelObject=roots, elr=modelXbrl.roleTypeDefinition(ELR), roots=", ".join(sorted((str(c.qname) for c in roots))))
        for labelrole, concepts in missingConceptLabels.items():
            modelXbrl.warning("ESEF.3.4.5.missingLabelForRoleInReportLanguage",
                _("Label for %(role)s role SHOULD be available in report language for concepts: %(qnames)s."),
                modelObject=concepts, qnames=", ".join(str(c.qname) for c in concepts), 
                role=os.path.basename(labelrole) if labelrole else "standard")
        if not pfsConceptsRootInPreLB:
            # no PFS statements were recognized
            modelXbrl.error("ESEF.RTS.Annex.II.Par.1.Par.7.missingPrimaryFinancialStatement",
                _("A primary financial statement placeholder element MUST be a root of a presentation linkbase tree."),
                modelObject=modelXbrl)
        # dereference
        del missingConceptLabels, pfsConceptsRootInPreLB
        
        # facts in declared units RTS Annex II para 1
        # assume declared currency is one with majority of concepts
        monetaryItemsNotInDeclaredCurrency = []
        unitCounts = sorted(statementMonetaryUnitFactCounts.items(), key=lambda uc:uc[1], reverse=True)
        if unitCounts: # must have a monetary statement fact for this check
            _declaredCurrency = unitCounts[0][0]
            for facts in modelXbrl.factsByQname.values():
                for f0 in facts:
                    concept = f0.concept
                    if concept is not None and concept.isMonetary:
                        hasDeclaredCurrency = False
                        for f in facts:
                            u = f.unit
                            if u is not None and u.isSingleMeasure and u.measures[0][0].localName == _declaredCurrency:
                                hasDeclaredCurrency = True
                                break
                        if not hasDeclaredCurrency:
                            monetaryItemsNotInDeclaredCurrency.append(concept)
                    break
        if monetaryItemsNotInDeclaredCurrency:
            modelXbrl.error("ESEF.RTS.Annex.II.Par.1.factsWithOtherThanDeclaredCurrencyOnly",
                _("Numbers SHALL be marked up in declared currency %(currency)s: %(qnames)s."),
                modelObject=monetaryItemsNotInDeclaredCurrency, currency=_declaredCurrency,
                qnames=", ".join(sorted(str(c.qname) for c in monetaryItemsNotInDeclaredCurrency)))
        
        # mandatory facts RTS Annex II
        missingMandatoryElements = esefMandatoryElements2020 - modelXbrl.factsByQname.keys()
        if missingMandatoryElements:
            modelXbrl.warning("ESEF.RTS.Annex.II.Par.2.missingMandatoryMarkups",
                _("Mandatory elements to be marked up are missing: %(qnames)s."),
                modelObject=missingMandatoryElements, qnames=", ".join(sorted(str(qn) for qn in missingMandatoryElements)))
        
        # duplicated core taxonomy elements  
        for name, concepts in modelXbrl.nameConcepts.items():
            if len(concepts) > 1:
                i = None # ifrs Concept
                for c in concepts:
                    if c.qname.namespaceURI == _ifrsNs:
                        i = c
                        break
                if i is not None:
                    for c in concepts:
                        if c != i and c.balance == i.balance and c.periodType == i.periodType:
                            modelXbrl.error("ESEF.RTS.Annex.IV.Par.4.1.extensionElementDuplicatesCoreElement",
                        _("Extension elements must not duplicate the existing elements from the core taxonomy and be identifiable %(qname)s."), 
                        modelObject=(c,i), qname=c.qname)

    modelXbrl.profileActivity(_statusMsg, minTimeToShow=0.0)
    modelXbrl.modelManager.showStatus(None)

def validateFinally(val, *args, **kwargs): # runs all inline checks
    if not (val.validateESEFplugin):
        return
    
    if val.unconsolidated:
        return

    modelXbrl = val.modelXbrl
    modelDocument = getattr(modelXbrl, "modelDocument")
    if (modelDocument is None or not modelXbrl.facts) and "ESEF.RTS.Art.6.a.noInlineXbrlTags" not in modelXbrl.errors:
        modelXbrl.error("ESEF.RTS.Art.6.a.noInlineXbrlTags",
                        _("RTS on ESEF requires inline XBRL, no facts were reported."),
                        modelObject=modelXbrl)
        return # never loaded properly

    numXbrlErrors = sum(ixErrorPattern.match(e) is not None for e in modelXbrl.errors if isinstance(e,str))
    if numXbrlErrors:
        modelXbrl.error("ESEF.RTS.Annex.III.Par.1.invalidInlineXBRL",
                        _("RTS on ESEF requires valid XBRL instances, %(numXbrlErrors)s errors were reported."), 
                        modelObject=modelXbrl, numXbrlErrors=numXbrlErrors)
        
    # force reporting of unsatisfied assertions for which there are no messages
    traceUnmessagedUnsatisfiedAssertions = True
    
def validateFormulaCompiled(modelXbrl, xpathContext):
    # request unsatisfied assertions without a message to print a trace
    xpathContext.formulaOptions.traceUnmessagedUnsatisfiedAssertions = True
        
def validateFormulaFinished(val, *args, **kwargs): # runs *after* formula (which is different for test suite from other operation
    if not getattr(val.modelXbrl.modelManager.disclosureSystem, "ESEFplugin", False):
        return

    modelXbrl = val.modelXbrl
    sumWrnMsgs = sumErrMsgs = 0
    for e in modelXbrl.errors:
        if isinstance(e,dict):
            for id, (numSat, numUnsat, numOkMsgs, numWrnMsgs, numErrMsgs) in e.items():
                sumWrnMsgs += numWrnMsgs
                sumErrMsgs += numErrMsgs
    if sumErrMsgs:
        modelXbrl.error("ESEF.2.7.1.targetXBRLDocumentWithFormulaErrors",
                        _("Target XBRL document MUST be valid against the assertions specified in ESEF taxonomy, %(numUnsatisfied)s with errors."), 
                        modelObject=modelXbrl, numUnsatisfied=sumErrMsgs)
    if sumWrnMsgs:
        modelXbrl.warning("ESEF.2.7.1.targetXBRLDocumentWithFormulaWarnings",
                        _("Target XBRL document SHOULD be valid against the assertions specified in ESEF taxonomy, %(numUnsatisfied)s with warnings."), 
                        modelObject=modelXbrl, numUnsatisfied=sumWrnMsgs)


def testcaseVariationReportPackageIxdsOptions(validate, rptPkgIxdsOptions):
    if getattr(validate.modelXbrl.modelManager.disclosureSystem, "ESEFplugin", False):
        rptPkgIxdsOptions["lookOutsideReportsDirectory"] = True
        rptPkgIxdsOptions["combineIntoSingleIxds"] = True
        
__pluginInfo__ = {
    # Do not use _( ) in pluginInfo itself (it is applied later, after loading
    'name': 'Validate ESMA ESEF',
    'version': '1.2020.03',
    'description': '''ESMA ESEF Filer Manual and RTS Validations.''',
    'license': 'Apache-2',
    'author': 'Mark V Systems',
    'copyright': '(c) Copyright 2018-20 Mark V Systems Limited, All rights reserved.',
    'ModelDocument.PullLoader': modelXbrlBeforeLoading,
    'import': ('inlineXbrlDocumentSet', ), # import dependent modules
    # classes of mount points (required)
    'DisclosureSystem.Types': dislosureSystemTypes,
    'DisclosureSystem.ConfigURL': disclosureSystemConfigURL,
    'ModelXbrl.LoadComplete': modelXbrlLoadComplete,
    'Validate.XBRL.Start': validateXbrlStart,
    'Validate.XBRL.Finally': validateXbrlFinally, # before formula processing
    'ValidateFormula.Compiled': validateFormulaCompiled,
    'ValidateFormula.Finished': validateFormulaFinished, # after formula processing
    'Validate.Finally': validateFinally, # run *after* formula processing
    'ModelTestcaseVariation.ReportPackageIxdsOptions': testcaseVariationReportPackageIxdsOptions,
}