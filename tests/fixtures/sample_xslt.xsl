<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="2.0"
                xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                xmlns:f="http://example.com/functions">

  <xsl:import href="sample_xslt_common.xsl"/>
  <xsl:include href="https://example.com/remote.xsl"/>
  <xsl:output method="html"/>
  <xsl:key name="by-id" match="item" use="@id"/>

  <!-- The processor picks this one: nothing here calls it. -->
  <xsl:template match="/">
    <html>
      <xsl:call-template name="page.header">
        <xsl:with-param name="title" select="'Report'"/>
      </xsl:call-template>
      <xsl:apply-templates select="report/section"/>
      <xsl:apply-templates select="report/footer" mode="plain"/>
      <xsl:apply-templates/>
      <xsl:call-template name="shared.footer"/>
    </html>
  </xsl:template>

  <xsl:template name="page.header">
    <xsl:param name="title"/>
    <xsl:param name="level" select="1"/>
    <h1><xsl:value-of select="$title"/></h1>
  </xsl:template>

  <xsl:template match="section | appendix">
    <div>
      <xsl:apply-templates select="title" mode="toc"/>
      <xsl:apply-templates mode="toc"/>
    </div>
  </xsl:template>

  <xsl:template match="title" mode="toc">
    <xsl:call-template name="page.header"/>
  </xsl:template>

  <xsl:template match="para[@role='note']" mode="toc" priority="2">
    <em><xsl:apply-templates select="key('by-id', @ref)"/></em>
  </xsl:template>

  <xsl:template match="para" mode="toc">
    <p/>
  </xsl:template>

  <xsl:template match="para" mode="toc" priority="0.5">
    <p/>
  </xsl:template>

  <xsl:template match="footer" mode="plain">
    <xsl:apply-templates mode="#current"/>
  </xsl:template>

  <xsl:function name="f:clean">
    <xsl:param name="value"/>
    <xsl:call-template name="page.header"/>
  </xsl:function>
</xsl:stylesheet>
